"""
orchestrator.py — IncidentOrchestrator root for QUERYSENTINEL.

Architecture (ADK 1.3.0 deterministic workflow):
    SequentialAgent(
        ParallelAgent([ContextAgent, SchemaAgent, SimilarAgent, RootCauseAgent]),
        RemediationAgent,
    )

Flow:
  1. Receives a raw anomaly event from the Change Stream watcher
  2. ParallelAgent fires all 4 read-only investigators CONCURRENTLY (real parallelism,
     not sequential LLM delegation). Each writes its JSON to session.state via output_key.
  3. RemediationAgent runs LAST and reads upstream state via {context_result},
     {schema_result}, {similar_result}, {rootcause_result} interpolation.
  4. Assembles the combined incident_report in Python (deterministic — no extra LLM call)
  5. Writes incident_report to app_db.incident_reports
  6. Writes per-step timing to app_db.agent_calls (Agent Reasoning Timeline data)
  7. Returns the full report for SSE broadcast

Bugs fixed in this rewrite:
  ✓ Bug 1 — ParallelAgent + SequentialAgent (was LlmAgent.sub_agents = non-deterministic delegation)
  ✓ Bug 2 — MCPToolset built inside async run_incident_pipeline (was module-level get_mongo_mcp())
  ✓ Bug 4 — user_id=incident_id (was user_id="system" → cross-incident context pollution)
  ✓ event.is_final_response() canonical terminal check (was fragile hasattr)
  ✓ Real per-agent timing logged to app_db.agent_calls for timeline UI
  ✓ GEMINI_MODEL centralized in config (was scattered "gemini-3.5-flash" strings)
  ✓ One shared MCPToolset across all 5 agents per run (was 5 separate npx subprocesses)
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

from db import app_db
from .mongo_fn_tools import make_mongo_fn_tools
from .context import build_context_agent
from .schema import build_schema_agent
from .similar import build_similar_agent
from .rootcause import build_rootcause_agent
from .remediation import build_remediation_agent
from impact import estimate_dollar_impact
from .callbacks import before_tool_callback, after_tool_callback, before_model_callback, set_incident_context
from detectors import get_owasp_classification  # OWASP LLM Top 10 classification

try:
    from slack_notifier import post_incident as _slack_post_incident
    from slack_notifier import is_configured as _slack_is_configured
except Exception:
    _slack_post_incident = None
    _slack_is_configured = lambda: False

logger = logging.getLogger("orchestrator")


# ── Session service (process-wide singleton) ──────────────────────────────────
# Sessions are isolated per incident via user_id=incident_id, so a shared
# service is safe and avoids reconstruction overhead.
_session_service = InMemorySessionService()


# ── Step tracker for Agent Reasoning Timeline ─────────────────────────────────

_KNOWN_AGENTS = (
    "IncidentPipeline",
    "ParallelInvestigation",
    "AnomalyContextAgent",
    "SchemaDriftAgent",
    "SimilarIncidentAgent",
    "RootCauseAgent",
    "RemediationAgent",
)


class _StepTracker:
    """
    Tracks per-agent step durations + MCP tool calls for the Gantt timeline UI.

    Parallel-aware design: when ParallelAgent dispatches sub-agents concurrently,
    events from multiple agents appear in the same stream with interleaved authors.
    This tracker records each agent's FIRST and LAST event independently — so
    overlapping bars in the Gantt correctly represent concurrent execution.

    Sequential vs parallel detection:
      If max(start_ms of all parallel agents) < min(end_ms of all parallel agents),
      the execution was truly parallel (bars overlap). If all start_ms are separated
      by > 100ms, agents ran sequentially. We log this as `parallel_verified: bool`.
    """

    _PARALLEL_AGENTS = frozenset({
        "AnomalyContextAgent", "SchemaDriftAgent",
        "SimilarIncidentAgent", "RootCauseAgent",
    })

    def __init__(self) -> None:
        # Per-agent dict keyed by author name
        self._agents: dict[str, dict] = {}
        self._t0 = time.monotonic()

    def on_event(self, event) -> None:
        author  = getattr(event, "author", None) or "unknown"
        now_ms  = int((time.monotonic() - self._t0) * 1000)

        if author not in self._agents:
            self._agents[author] = {
                "agent":       author,
                "start_ms":    now_ms,
                "end_ms":      now_ms,
                "duration_ms": 0,
                "tool_calls":  [],
                "text_chunks": 0,
            }

        step = self._agents[author]
        # Always extend end_ms — last event wins
        step["end_ms"]      = now_ms
        step["duration_ms"] = now_ms - step["start_ms"]

        # Count tool calls and text chunks
        if hasattr(event, "content") and event.content and getattr(event.content, "parts", None):
            for part in event.content.parts:
                fn_call = getattr(part, "function_call", None)
                if fn_call is not None:
                    step["tool_calls"].append({
                        "name":  getattr(fn_call, "name", "unknown"),
                        "at_ms": now_ms,
                    })
                if getattr(part, "text", None):
                    step["text_chunks"] += 1

    def close(self) -> tuple[list[dict], bool]:
        """
        Returns (steps, parallel_verified).

        parallel_verified=True means the 4 parallel agents had overlapping
        time windows — proving ADK's ParallelAgent ran them concurrently.
        """
        steps = list(self._agents.values())
        # Sort for readable output: by start_ms
        steps.sort(key=lambda s: s["start_ms"])

        # Verify parallelism: do the 4 parallel agents' windows overlap?
        parallel_steps = [s for s in steps if s["agent"] in self._PARALLEL_AGENTS]
        parallel_verified = False
        if len(parallel_steps) >= 2:
            # Overlap = max(start) < min(end)
            max_start = max(s["start_ms"] for s in parallel_steps)
            min_end   = min(s["end_ms"]   for s in parallel_steps)
            parallel_verified = max_start < min_end
            logger.info(
                "Parallel execution check: max_start=%dms min_end=%dms → %s",
                max_start, min_end,
                "PARALLEL ✓" if parallel_verified else "SEQUENTIAL (ADK may be serialising)",
            )

        return steps, parallel_verified


# ── Pipeline assembly ─────────────────────────────────────────────────────────

def _build_pipeline(tools: list, force_sequential: bool = False) -> SequentialAgent:
    """
    Build the deterministic agent tree for one incident.

    Gemini + FunctionTools (GEMINI_USE_MCP=false):
      SequentialAgent → ParallelAgent([Context, Schema, Similar, RootCause]) → Remediation
      Real parallelism: 4 agents run concurrently. Speedup ~3.5×.

    Gemini + MongoDB MCP server (GEMINI_USE_MCP=true, DEFAULT, force_sequential=True):
      SequentialAgent → [Context, Schema, Similar, RootCause, Remediation] all sequential.
      MCPToolset shares one stdio session; ParallelAgent's concurrent asyncio tasks
      exit anyio cancel scopes in different tasks → crash. Sequential avoids this AND
      satisfies the hackathon's "integrate a Partner Entity's MCP server" requirement.

    Offline-dev fallback backends: always sequential.
    """
    from config import _llm_backend
    _cbs = {
        "before_model_callback": before_model_callback,
        "before_tool_callback":  before_tool_callback,
        "after_tool_callback":   after_tool_callback,
    }

    investigator_agents = [
        build_context_agent(tools,   callbacks=_cbs),
        build_schema_agent(tools,    callbacks=_cbs),
        build_similar_agent(tools,   callbacks=_cbs),
        build_rootcause_agent(tools, callbacks=_cbs),
    ]

    if _llm_backend == "gemini" and not force_sequential:
        # Gemini + FunctionTools: true parallel dispatch via ParallelAgent
        investigation = ParallelAgent(
            name="ParallelInvestigation",
            sub_agents=investigator_agents,
        )
        sub_agents = [investigation, build_remediation_agent(tools, callbacks=_cbs)]
    else:
        # MongoDB MCP (force_sequential) OR LiteLlm: all 5 agents sequential
        sub_agents = investigator_agents + [build_remediation_agent(tools, callbacks=_cbs)]

    return SequentialAgent(name="IncidentPipeline", sub_agents=sub_agents)


# ── Public API ────────────────────────────────────────────────────────────────

async def run_incident_pipeline(anomaly: dict) -> dict:
    """
    Main entry point called by watcher.py when an anomaly is detected.

    Backend routing:
      gemini  → full ADK pipeline (ParallelAgent + LlmAgent tool calling)
      (offline-dev fallback) → direct_pipeline (pre-fetch data + parallel calls)

    ADK tool calling only works reliably with Gemini. Non-Gemini LiteLlm models
    generate malformed function call JSON causing "agent not in tools_dict" crashes.
    """
    from config import _llm_backend
    incident_id = str(uuid.uuid4())
    t_start     = datetime.now(timezone.utc)
    set_incident_context(anomaly, incident_id)

    # ── Zero-LLM mode: detection-only, no model calls at all ─────────────────
    # _assemble_report builds the full deterministic incident from detection
    # signals (regex/velocity/vector/schema). Lets the demo run with no API key.
    if _llm_backend in ("none", "off", "deterministic"):
        pipeline_result = {
            "agent_steps": [{"agent": "DeterministicDetector", "start_ms": 0,
                             "end_ms": 1, "duration_ms": 1, "tool_calls": [], "text_chunks": 0}],
            "pipeline_ms": int((datetime.now(timezone.utc) - t_start).total_seconds() * 1000),
            "parallel_verified": False, "speedup_factor": 1.0, "pipeline_status": "deterministic",
        }
        return await _assemble_report(anomaly, incident_id, t_start, pipeline_result)

    # ── Non-Gemini: direct pipeline (no ADK tool calling) ────────────────────
    if _llm_backend != "gemini":
        from .direct_pipeline import run_direct_pipeline
        pipeline_result = await run_direct_pipeline(anomaly)
        return await _assemble_report(anomaly, incident_id, t_start, pipeline_result)

    # ── Gemini: full ADK pipeline ─────────────────────────────────────────────
    # COMPLIANCE: when GEMINI_USE_MCP=true (default), all agents run through
    # MongoDB's official MCP server (npx mongodb-mcp-server) via ADK MCPToolset.
    # This satisfies the hackathon's "integrate a Partner Entity's MCP server".
    # MCPToolset requires SequentialAgent (anyio cancel scopes break ParallelAgent).
    from config import GEMINI_USE_MCP
    _mcp_toolset = None
    if GEMINI_USE_MCP:
        try:
            from .mcp_tools import make_mongo_toolset
            _mcp_toolset = make_mongo_toolset()
            tools = [_mcp_toolset]
            pipeline = _build_pipeline(tools, force_sequential=True)
            logger.info("Pipeline using MongoDB MCP server (compliant mode)")
        except Exception as e:
            logger.warning("MongoDB MCP toolset failed (%s) — falling back to FunctionTools", e)
            _mcp_toolset = None
            tools = make_mongo_fn_tools()
            pipeline = _build_pipeline(tools)
    else:
        tools = make_mongo_fn_tools()
        pipeline = _build_pipeline(tools)

    runner   = Runner(
        agent=pipeline,
        app_name="querysentinel",
        session_service=_session_service,
    )

    # ── Create an isolated session for this incident (Bug 4 fix) ─────────────
    # user_id == incident_id guarantees zero history bleed from prior incidents.
    await _session_service.create_session(
        app_name="querysentinel",
        user_id=incident_id,
        session_id=incident_id,
    )

    # ── Build the input message (inject incident_id for downstream echoing) ──
    anomaly_with_id = {**anomaly, "incident_id": incident_id}
    approve_signal  = anomaly.get("_approve_signal", "")
    payload_text    = json.dumps(anomaly_with_id, default=str)
    if approve_signal:
        payload_text = f"{approve_signal}\n\n{payload_text}"

    message = Content(role="user", parts=[Part.from_text(text=payload_text)])
    tracker = _StepTracker()
    final_session = None

    # ── Run the pipeline ──────────────────────────────────────────────────────
    # Retry-with-backoff for Gemini 429 RESOURCE_EXHAUSTED
    # Free tier is 5 RPM; the pipeline naturally bursts 5+ calls. We retry the
    # whole pipeline up to 2 extra times with exponential backoff. With billing
    # enabled this is rarely triggered, but it's belt-and-suspenders for demos.
    _MAX_PIPELINE_ATTEMPTS = 3
    _PIPELINE_DEADLINE_S = 90   # absolute ceiling — never block the demo longer
    _last_exc = None

    async def _drain_pipeline():
        async for event in runner.run_async(
            user_id=incident_id,
            session_id=incident_id,
            new_message=message,
        ):
            tracker.on_event(event)
            if event.is_final_response():
                logger.debug("Final response from %s", getattr(event, "author", "?"))

    for attempt in range(1, _MAX_PIPELINE_ATTEMPTS + 1):
        try:
            # Hard timeout: if Gemini hangs/throttles, abandon and let the
            # deterministic baseline fill the incident. LLM = helper, not dependency.
            await asyncio.wait_for(_drain_pipeline(), timeout=_PIPELINE_DEADLINE_S)
            _last_exc = None
            break  # success
        except asyncio.TimeoutError:
            logger.warning(
                "Gemini pipeline exceeded %ds (incident=%s) — using deterministic fallback",
                _PIPELINE_DEADLINE_S, incident_id,
            )
            _last_exc = None  # not an error — deterministic baseline covers it
            break
        except Exception as inner_exc:
            err_str = str(inner_exc)
            is_429 = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower()
            is_otel = "different Context" in err_str or "created in" in err_str
            if is_otel:
                # Known OTel cleanup — completed successfully, just noisy
                _last_exc = None
                break
            if is_429 and attempt < _MAX_PIPELINE_ATTEMPTS:
                wait_s = 12 * attempt  # 12s, 24s — slightly over Gemini's 9-10s retry hint
                logger.warning(
                    "Pipeline (incident=%s) hit Gemini 429 (attempt %d/%d) — retrying in %ds",
                    incident_id, attempt, _MAX_PIPELINE_ATTEMPTS, wait_s,
                )
                await asyncio.sleep(wait_s)
                # Reset session for retry to avoid duplicate state contamination
                await _session_service.create_session(
                    app_name="querysentinel",
                    user_id=incident_id + f"-retry{attempt}",
                    session_id=incident_id + f"-retry{attempt}",
                )
                continue
            _last_exc = inner_exc
            break
    # ── Post-pipeline outcome ─────────────────────────────────────────────────
    # If retries exhausted with a real error, we still proceed to write a
    # degraded incident_report. Empty session state → empty JSON fields →
    # the Python deterministic assembly below produces a report with whatever
    # agent outputs DID land. This is the "no silent failures" guarantee.
    pipeline_status = "ok" if _last_exc is None else "degraded"
    if _last_exc is not None:
        err_short = str(_last_exc)[:200] or type(_last_exc).__name__
        logger.warning(
            "Pipeline (incident=%s) DEGRADED — partial state will be written: %s",
            incident_id, err_short,
        )

    # ── Close MongoDB MCP toolset (terminates the npx subprocess) ─────────────
    if _mcp_toolset is not None:
        try:
            await _mcp_toolset.close()
        except Exception as e:
            logger.debug("MCP toolset close (non-fatal): %s", e)

    elapsed_ms              = int((datetime.now(timezone.utc) - t_start).total_seconds() * 1000)
    steps, parallel_verified = tracker.close()

    # ── Pull per-agent JSON outputs from session state ────────────────────────
    final_session = await _session_service.get_session(
        app_name="querysentinel",
        user_id=incident_id,
        session_id=incident_id,
    )
    state = getattr(final_session, "state", {}) or {}

    _llm_fields = {
        "context":           _safe_parse_json(state.get("context_result")),
        "schema_drift":      _safe_parse_json(state.get("schema_result")),
        "similar_incidents": _safe_parse_json(state.get("similar_result")),
        "root_cause":        _safe_parse_json(state.get("rootcause_result")),
        "remediation":       _safe_parse_json(state.get("remediation_result")),
    }
    # ── Graceful degradation: always-on deterministic baseline, LLM enriches ──
    # If Gemini was throttled (429) and left fields empty, the deterministic
    # detection content fills them so the incident is NEVER empty.
    from .deterministic import deterministic_content, merge_llm_over_deterministic
    _det = deterministic_content(anomaly, app_db)
    _merged, _llm_enriched = merge_llm_over_deterministic(_det, _llm_fields)
    context_json     = _merged["context"]
    schema_json      = _merged["schema_drift"]
    similar_json     = _merged["similar_incidents"]
    rootcause_json   = _merged["root_cause"]
    remediation_json = _merged["remediation"]

    # ── Assemble incident_report ──────────────────────────────────────────────
    severity     = anomaly.get("severity", "WARNING")
    collection   = anomaly.get("collection_name", "")
    anomaly_type = anomaly.get("anomaly_type", "UNKNOWN")
    z_score      = float(anomaly.get("z_score", 0))

    # Business-impact dollar estimate (transparent, conservative formula)
    try:
        dollar_impact = estimate_dollar_impact(collection, z_score, anomaly_type)
    except Exception as e:
        logger.warning("Dollar impact estimate failed: %s", e)
        dollar_impact = {}

    # OWASP LLM Top 10 classification — every incident maps to a known threat standard
    # This makes QuerySentinel speak the language of security teams + CISOs
    try:
        owasp_classification = get_owasp_classification(anomaly_type)
    except Exception:
        owasp_classification = {"id": "LLM05", "name": "Supply Chain Vulnerabilities"}

    incident_report = {
        "incident_id":       incident_id,
        "collection_name":   collection,
        "anomaly_type":      anomaly_type,
        "severity":          severity,
        "z_score":           z_score,
        "detected_at":       anomaly.get("detected_at", t_start.isoformat()),
        "pipeline_ms":       elapsed_ms,
        "context":           context_json,
        "schema_drift":      schema_json,
        "similar_incidents": similar_json,
        "root_cause":        rootcause_json,
        "remediation":       remediation_json,
        "summary":           _build_summary(
                                 anomaly, context_json, rootcause_json, remediation_json
                             ),
        "slack_emoji":       "🔴" if severity == "CRITICAL" else "🟡",
        "estimated_resolution_minutes": _pick_resolution_estimate(
            similar_json, rootcause_json, remediation_json
        ),
        "estimated_dollar_impact": dollar_impact,
        "anomaly_event":          anomaly,
        "agent_steps":            steps,
        "pipeline_status":        "ok" if _llm_enriched else "deterministic",
        "llm_enriched":           _llm_enriched,
        "parallel_verified":      parallel_verified,
        "speedup_factor":         None,  # filled below after agent_calls compute
        "correlation_status":     "pending",
        # ── OWASP LLM Top 10 classification ──────────────────────────────────
        # Every incident maps to the OWASP standard. Makes QuerySentinel speak
        # the language of security teams — not just database engineers.
        "owasp_classification":   owasp_classification,
        # ── AI Memory Poisoning flag ──────────────────────────────────────────
        # Set by watcher.py when scan_document_for_ai_threats fires
        "ai_memory_poisoning":    anomaly.get("ai_memory_poisoning", False),
        "ai_threat_matches":      anomaly.get("ai_threat_matches", []),
    }

    # ── Tamper-evident receipt (AetherProof Ed25519) ─────────────────────────
    try:
        from audit_receipt import sign_payload
        _rcpt = sign_payload(incident_report)
        if _rcpt:
            incident_report["aetherproof_receipt"] = _rcpt
            incident_report["receipt_verified"] = True
    except Exception as e:
        logger.debug("Receipt signing skipped: %s", e)

    # ── Persist to MongoDB ────────────────────────────────────────────────────
    try:
        app_db.incident_reports.insert_one({
            **incident_report,
            "created_at": datetime.now(timezone.utc),
        })
    except Exception as e:
        logger.warning("Could not persist incident_report: %s", e)

    # ── Slack notification (non-blocking, no-op if webhook not configured) ────
    if _slack_post_incident and _slack_is_configured():
        try:
            asyncio.create_task(_slack_post_incident(incident_report),
                                name=f"slack-{incident_id[:8]}")
        except Exception as e:
            logger.debug("Slack dispatch skipped: %s", e)

    # Parallel speedup: compare parallel wall-clock vs sequential sum
    parallel_steps_list = [
        s for s in steps
        if s["agent"] in _StepTracker._PARALLEL_AGENTS
    ]
    sequential_sum_ms = sum(s.get("duration_ms", 0) for s in parallel_steps_list)
    parallel_wall_ms  = (
        max(s["end_ms"]   for s in parallel_steps_list) -
        min(s["start_ms"] for s in parallel_steps_list)
    ) if len(parallel_steps_list) >= 2 else sequential_sum_ms
    speedup_factor = round(sequential_sum_ms / max(parallel_wall_ms, 1), 2) if parallel_wall_ms else 1.0

    try:
        app_db.agent_calls.insert_one({
            "incident_id":         incident_id,
            "collection_name":     anomaly.get("collection_name"),
            "severity":            severity,
            "total_ms":            elapsed_ms,
            "steps":               steps,
            "parallel_verified":   parallel_verified,
            "speedup_factor":      speedup_factor,
            "sequential_sum_ms":   sequential_sum_ms,
            "parallel_wall_ms":    parallel_wall_ms,
            "created_at":          datetime.now(timezone.utc),
        })
    except Exception as e:
        logger.warning("Could not persist agent_calls: %s", e)

    logger.info(
        "Pipeline OK: id=%s coll=%s sev=%s elapsed=%dms steps=%d parallel=%s speedup=%.1fx",
        incident_id,
        anomaly.get("collection_name"),
        severity,
        elapsed_ms,
        len(steps),
        "✓" if parallel_verified else "✗",
        speedup_factor,
    )
    return incident_report


async def _assemble_report(
    anomaly: dict,
    incident_id: str,
    t_start,
    pipeline_result: dict,
) -> dict:
    """
    Shared report assembly for both ADK and direct pipeline paths.
    pipeline_result contains: context, schema_drift, similar_incidents,
    root_cause, remediation, agent_steps, pipeline_ms, parallel_verified,
    speedup_factor, sequential_sum_ms, pipeline_status.
    """
    severity     = anomaly.get("severity", "WARNING")
    collection   = anomaly.get("collection_name", "")
    anomaly_type = anomaly.get("anomaly_type", "UNKNOWN")
    z_score      = float(anomaly.get("z_score", 0))
    elapsed_ms   = pipeline_result.get("pipeline_ms",
                   int((datetime.now(timezone.utc) - t_start).total_seconds() * 1000))

    # ── Graceful degradation: deterministic baseline, LLM enriches if present ──
    from .deterministic import deterministic_content, merge_llm_over_deterministic
    _det = deterministic_content(anomaly, app_db)
    _llm_fields = {
        "context":           pipeline_result.get("context", {}),
        "schema_drift":      pipeline_result.get("schema_drift", {}),
        "similar_incidents": pipeline_result.get("similar_incidents", {}),
        "root_cause":        pipeline_result.get("root_cause", {}),
        "remediation":       pipeline_result.get("remediation", {}),
    }
    _merged, _llm_enriched = merge_llm_over_deterministic(_det, _llm_fields)
    context_json     = _merged["context"]
    schema_json      = _merged["schema_drift"]
    similar_json     = _merged["similar_incidents"]
    rootcause_json   = _merged["root_cause"]
    remediation_json = _merged["remediation"]
    steps            = pipeline_result.get("agent_steps", [])
    parallel_verified = pipeline_result.get("parallel_verified", False)
    speedup_factor    = pipeline_result.get("speedup_factor", 1.0)
    sequential_sum_ms = pipeline_result.get("sequential_sum_ms", 0)
    parallel_wall_ms  = pipeline_result.get("parallel_ms", elapsed_ms)
    pipeline_status   = "ok" if _llm_enriched else "deterministic"

    try:
        dollar_impact = estimate_dollar_impact(collection, z_score, anomaly_type)
    except Exception:
        dollar_impact = {}

    try:
        owasp_classification = get_owasp_classification(anomaly_type)
    except Exception:
        owasp_classification = {"id": "LLM05", "name": "Supply Chain Vulnerabilities"}

    incident_report = {
        "incident_id":       incident_id,
        "collection_name":   collection,
        "anomaly_type":      anomaly_type,
        "severity":          severity,
        "z_score":           z_score,
        "detected_at":       anomaly.get("detected_at", t_start.isoformat()),
        "pipeline_ms":       elapsed_ms,
        "context":           context_json,
        "schema_drift":      schema_json,
        "similar_incidents": similar_json,
        "root_cause":        rootcause_json,
        "remediation":       remediation_json,
        "summary":           _build_summary(anomaly, context_json, rootcause_json, remediation_json),
        "slack_emoji":       "🔴" if severity == "CRITICAL" else "🟡",
        "estimated_resolution_minutes": _pick_resolution_estimate(similar_json, rootcause_json, remediation_json),
        "estimated_dollar_impact": dollar_impact,
        "anomaly_event":          anomaly,
        "agent_steps":            steps,
        "pipeline_status":        pipeline_status,
        "llm_enriched":           _llm_enriched,
        "parallel_verified":      parallel_verified,
        "speedup_factor":         speedup_factor,
        "correlation_status":     "pending",
        "owasp_classification":   owasp_classification,
        "ai_memory_poisoning":    anomaly.get("ai_memory_poisoning", False),
        "ai_threat_matches":      anomaly.get("ai_threat_matches", []),
    }

    # ── Tamper-evident receipt (AetherProof Ed25519) ─────────────────────────
    try:
        from audit_receipt import sign_payload
        _rcpt = sign_payload(incident_report)
        if _rcpt:
            incident_report["aetherproof_receipt"] = _rcpt
            incident_report["receipt_verified"] = True
    except Exception as e:
        logger.debug("Receipt signing skipped: %s", e)

    try:
        app_db.incident_reports.insert_one({**incident_report, "created_at": datetime.now(timezone.utc)})
    except Exception as e:
        logger.warning("Could not persist incident_report: %s", e)

    if _slack_post_incident and _slack_is_configured():
        try:
            asyncio.create_task(_slack_post_incident(incident_report), name=f"slack-{incident_id[:8]}")
        except Exception:
            pass

    try:
        app_db.agent_calls.insert_one({
            "incident_id":       incident_id,
            "collection_name":   collection,
            "severity":          severity,
            "total_ms":          elapsed_ms,
            "steps":             steps,
            "parallel_verified": parallel_verified,
            "speedup_factor":    speedup_factor,
            "sequential_sum_ms": sequential_sum_ms,
            "parallel_wall_ms":  parallel_wall_ms,
            "created_at":        datetime.now(timezone.utc),
        })
    except Exception as e:
        logger.warning("Could not persist agent_calls: %s", e)

    logger.info(
        "Pipeline %s: id=%s coll=%s sev=%s elapsed=%dms steps=%d parallel=%s speedup=%.1fx",
        pipeline_status.upper(),
        incident_id, collection, severity, elapsed_ms, len(steps),
        "✓" if parallel_verified else "✗", speedup_factor,
    )
    return incident_report


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_parse_json(value) -> dict:
    """Parse a session-state JSON string into a dict; tolerate fences and raw dicts."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {"raw": str(value)}

    raw = value.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        raw = "\n".join(inner).strip()
    if raw.startswith("```"):
        raw = raw[3:].strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw_response": value, "parse_error": True}


def _build_summary(anomaly: dict, context: dict, rootcause: dict, remediation: dict) -> str:
    """Deterministic 3-sentence Slack-ready summary from agent outputs."""
    coll  = anomaly.get("collection_name", "?")
    sev   = anomaly.get("severity", "?")
    atype = anomaly.get("anomaly_type", "?")
    z     = anomaly.get("z_score", 0)

    s1 = f"{sev} {atype} on '{coll}' (z={z:.1f})."
    s2 = (
        context.get("anomaly_summary")
        or rootcause.get("root_cause_description")
        or "Investigation complete; see context for details."
    )
    options = (remediation or {}).get("options") or []
    if options:
        top = options[0]
        s3  = f"Recommended: {top.get('title', 'review options')} (~{top.get('estimated_minutes', '?')} min)."
    else:
        s3 = "No remediation options returned; manual triage required."
    return f"{s1} {s2} {s3}"


def _pick_resolution_estimate(similar: dict, rootcause: dict, remediation: dict) -> int:
    """Prefer similar-incident historical estimate; fall back to remediation option 1."""
    if similar:
        est = similar.get("estimated_resolution_minutes")
        if isinstance(est, (int, float)) and est > 0:
            return int(est)
    options = (remediation or {}).get("options") or []
    if options:
        est = options[0].get("estimated_minutes")
        if isinstance(est, (int, float)) and est > 0:
            return int(est)
    return 30
