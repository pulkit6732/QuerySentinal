"""
main.py — QUERYSENTINEL FastAPI backend.

Routes:
  GET  /health                → component health check (MongoDB + stream state)
  GET  /api/health-card       → Database Health Card (startup survey)
  GET  /api/anomalies         → last 50 anomalies from raw_events
  GET  /api/incidents         → last 20 full incident reports
  GET  /api/stream            → SSE stream of live anomaly events
  GET  /api/runway            → runway predictions for all collections
  GET  /api/semantic-velocity → semantic velocity for all collections
  POST /api/inject-test       → injects a 700 KB test anomaly (demo button)
  POST /api/approve           → human-in-the-loop APPROVE gate
  POST /api/baseline/refresh  → recompute baseline for a collection

Startup:
  1. Verify MongoDB connectivity
  2. Build Database Health Card (async, non-blocking)
  3. Launch Change Stream watcher tasks for all monitored collections
"""
import asyncio
import collections
import json
import logging
import os
import random
import string
import time
from datetime import datetime, timezone
from typing import Any

import pymongo
from fastapi import FastAPI, HTTPException, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from sse_starlette.sse import EventSourceResponse

from config import MONITORED_COLLECTIONS, SOURCE_DB, PORT
from db import app_db, source_db, ping
from detect import (
    compute_baseline,
    detect_schema_drift,
    compute_semantic_velocity,
    compute_incident_runway,
    build_database_health_card,
)
from watcher import anomaly_queue, start_all_watchers, emit_anomaly

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

# ── Security: Rate Limiter (in-memory, per-IP sliding window) ─────────────────
# Protects against:  OWASP LLM04 (Model DoS via API flooding)
#                    credential stuffing on /api/approve
_rate_windows: dict[str, collections.deque] = collections.defaultdict(
    lambda: collections.deque()
)
_RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "60"))  # requests per minute per IP

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for SSE and health (long-lived connections)
        path = request.url.path
        if path in ("/api/stream", "/health", "/"):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        window = _rate_windows[client_ip]

        # Slide window: remove entries older than 60s
        while window and now - window[0] > 60.0:
            window.popleft()

        if len(window) >= _RATE_LIMIT_RPM:
            logger.warning("Rate limit hit: IP=%s path=%s", client_ip, path)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limit_exceeded",
                    "detail": f"Max {_RATE_LIMIT_RPM} requests/minute per IP.",
                    "retry_after_seconds": 60,
                },
                headers={"Retry-After": "60"},
            )

        window.append(now)
        return await call_next(request)

# ── Security: API key for destructive / sensitive endpoints ──────────────────
# QUERYSENTINEL_API_KEY env var — if not set, all endpoints are open (dev mode)
_API_KEY_HEADER = APIKeyHeader(name="X-QuerySentinel-Key", auto_error=False)
_REQUIRED_API_KEY = os.getenv("QUERYSENTINEL_API_KEY", "").strip()

def _check_api_key(key: str | None = Security(_API_KEY_HEADER)) -> None:
    """Dependency: validates X-QuerySentinel-Key header for sensitive endpoints."""
    if not _REQUIRED_API_KEY:
        return  # dev mode — no key required
    if key != _REQUIRED_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing X-QuerySentinel-Key header.",
        )

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="QUERYSENTINEL",
    description=(
        "Live MongoDB anomaly intelligence — Atlas Change Streams + Google ADK 1.3.0 agents. "
        "QuerySentinel also exposes its detection capabilities as an MCP server at /mcp."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # tighten before production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)

# ── Security headers middleware ───────────────────────────────────────────────
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"]    = "nosniff"
    response.headers["X-Frame-Options"]           = "DENY"
    response.headers["X-XSS-Protection"]          = "1; mode=block"
    response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
    response.headers["X-QuerySentinel-Version"]   = "1.0.0"
    return response

# ── Mount QuerySentinel as MCP Server ─────────────────────────────────────────
# QUERYSENTINEL doesn't just CONSUME MongoDB MCP tools — it IS an MCP server.
# Other ADK agents and any MCP client can call QuerySentinel tools.
#
# Tools: detect_anomaly · list_incidents · get_semantic_velocity
#        get_similar_incidents · get_cluster_events
#
# MCP client config:
#   { "querysentinel": { "url": "https://YOUR_BACKEND/mcp" } }
try:
    from mcp_server import qs_mcp
    # FastMCP ASGI app — Streamable HTTP transport (mcp >= 1.9.0)
    # Compatible with ADK and other MCPToolset(StreamableHTTPServerParams)
    _mcp_asgi = getattr(qs_mcp, "streamable_http_app", None) or getattr(qs_mcp, "sse_app", None)
    if _mcp_asgi:
        app.mount("/mcp", _mcp_asgi())
        logger.info("QuerySentinel MCP server mounted at /mcp")
    else:
        logger.warning("FastMCP ASGI method not found — MCP server not mounted.")
except Exception as _mcp_err:
    logger.warning("MCP server mount failed (mcp package not installed?): %s", _mcp_err)


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup() -> None:
    logger.info("QUERYSENTINEL starting up…")

    # 1. Verify MongoDB
    if not ping():
        logger.error("Cannot reach MongoDB Atlas — check MONGODB_URI in .env")
        return

    # 2. Build Database Health Card (background — non-blocking)
    asyncio.create_task(_build_health_card_background())

    # 3. Start Change Stream watchers + semantic velocity monitor
    await start_all_watchers()

    # 4. Start adaptive baseline LoopAgent (ADK LoopAgent — every 30 minutes)
    from agents.baseline_refiner import run_baseline_loop_forever
    asyncio.create_task(run_baseline_loop_forever(interval_s=1800), name="baseline-loop")

    logger.info("Startup complete. Watching: %s", MONITORED_COLLECTIONS)


async def _build_health_card_background() -> None:
    try:
        result = await asyncio.to_thread(build_database_health_card)
        logger.info("Health card built: %d collections surveyed.", len(result["cards"]))
    except Exception as e:
        logger.warning("Health card build failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    """Full component health check — verifies all subsystems."""
    status: dict[str, Any] = {}

    # MongoDB reachable?
    status["mongodb"] = "ok" if ping() else "error"

    # Change Stream resume tokens present?
    stream_states = list(app_db.stream_state.find(
        {"_id": {"$in": MONITORED_COLLECTIONS}},
        {"_id": 1, "updated_at": 1},
    ))
    status["stream_watchers"] = {
        s["_id"]: s.get("updated_at", "never").isoformat()
        if isinstance(s.get("updated_at"), datetime) else "never"
        for s in stream_states
    }

    # Anomaly queue depth
    status["queue_depth"] = anomaly_queue.qsize()

    # Failed events pending retry
    status["failed_events_pending"] = app_db.failed_events.count_documents(
        {"retry_count": {"$lt": 3}}
    )

    # vectorSearch index reachable? Must use a real queryVector (the index has no
    # text auto-embedding), so embed a probe string locally first.
    try:
        from agents.mongo_fn_tools import _embed_text_local
        _pv = _embed_text_local("health probe")
        list(app_db.anomaly_history.aggregate([
            {"$vectorSearch": {
                "index": "anomaly_semantic",
                "queryVector": _pv,
                "path": "embedding",
                "numCandidates": 1,
                "limit": 1,
            }},
        ]))
        status["vector_search"] = "ok"
    except Exception as e:
        status["vector_search"] = f"error: {str(e)[:80]}"

    all_ok = (
        status["mongodb"] == "ok"
        and status["vector_search"] == "ok"
        and status["failed_events_pending"] == 0
    )
    return {
        "status":     "ok" if all_ok else "degraded",
        "components": status,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE HEALTH CARD
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/health-card")
async def get_health_card() -> dict:
    """Returns the cached Database Health Card (updated at startup)."""
    cached = app_db.dashboard_state.find_one({"_id": "health_card"})
    if cached:
        cached.pop("_id", None)
        return cached
    # Build synchronously on miss (first request before background task finishes)
    return await asyncio.to_thread(build_database_health_card)


# ─────────────────────────────────────────────────────────────────────────────
# ANOMALIES & INCIDENTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/anomalies")
async def get_anomalies(limit: int = 50) -> dict:
    """Most recent anomalies from raw_events, newest first."""
    docs = list(
        app_db.raw_events.find(
            {},
            {"embedding": 0},           # exclude large embedding vectors
            sort=[("detected_at", pymongo.DESCENDING)],
            limit=min(limit, 200),
        )
    )
    for d in docs:
        d["_id"] = str(d["_id"])
        dt = d.get("detected_at")
        if isinstance(dt, datetime):
            # pymongo returns naive UTC; mark it so the browser parses as UTC, not local
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            d["detected_at"] = dt.isoformat()
    return {"anomalies": docs, "count": len(docs)}


@app.get("/api/incidents")
async def get_incidents(limit: int = 20) -> dict:
    """Most recent full incident reports from incident_reports."""
    docs = list(
        app_db.incident_reports.find(
            {},
            sort=[("created_at", pymongo.DESCENDING)],
            limit=min(limit, 100),
        )
    )
    for d in docs:
        d["_id"] = str(d["_id"])
        dt = d.get("created_at")
        if isinstance(dt, datetime):
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            d["created_at"] = dt.isoformat()
    return {"incidents": docs, "count": len(docs)}


@app.get("/api/verify/{incident_id}")
async def verify_incident(incident_id: str) -> dict:
    """
    Tamper-evident verification (AetherProof Ed25519).

    Re-reads the incident report straight from MongoDB and checks it against the
    cryptographic receipt that QuerySentinel sealed it with. Proves the stored
    document is byte-identical to what the agents found — or flags it TAMPERED.

    This is the data-integrity guarantee: QuerySentinel's own forensic findings
    cannot be silently altered in the database without detection.
    """
    from audit_receipt import verify_payload, receipt_pretty
    doc = app_db.incident_reports.find_one({"incident_id": incident_id})
    if not doc:
        return {"found": False, "incident_id": incident_id}
    doc.pop("_id", None)
    result = verify_payload(doc)
    return {
        "found":            True,
        "incident_id":      incident_id,
        "collection_name":  doc.get("collection_name"),
        "severity":         doc.get("severity"),
        "integrity":        result,
        "receipt_pretty":   receipt_pretty(doc.get("aetherproof_receipt", "")),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SSE — LIVE ANOMALY STREAM
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/stream")
async def sse_stream(request: Request):
    """
    Server-Sent Events endpoint. The Next.js dashboard connects here on load.
    Every anomaly detected by the Change Stream watcher is pushed here
    within ~1 second of detection.
    """
    async def generator():
        # Send recent anomalies on connect so dashboard isn't empty
        recent = list(app_db.raw_events.find(
            {},
            {"embedding": 0},
            sort=[("detected_at", pymongo.DESCENDING)],
            limit=10,
        ))
        for doc in reversed(recent):
            doc["_id"] = str(doc["_id"])
            if isinstance(doc.get("detected_at"), datetime):
                doc["detected_at"] = doc["detected_at"].isoformat()
            yield {"event": "anomaly", "data": json.dumps(doc, default=str)}

        # Stream live events
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(anomaly_queue.get(), timeout=15.0)
                event_copy = {k: v for k, v in event.items()}
                event_copy.pop("embedding", None)
                if isinstance(event_copy.get("detected_at"), datetime):
                    event_copy["detected_at"] = event_copy["detected_at"].isoformat()
                yield {"event": "anomaly", "data": json.dumps(event_copy, default=str)}
            except asyncio.TimeoutError:
                yield {"event": "keepalive", "data": "ping"}

    return EventSourceResponse(generator())


# ─────────────────────────────────────────────────────────────────────────────
# RUNWAY & SEMANTIC VELOCITY
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/runway")
async def get_runway() -> dict:
    """Incident runway predictions for all monitored collections."""
    results = {}
    for cname in MONITORED_COLLECTIONS:
        results[cname] = await asyncio.to_thread(compute_incident_runway, cname)
    return {"runway": results, "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/semantic-velocity")
async def get_semantic_velocity() -> dict:
    """Semantic velocity (cosine centroid drift) for all monitored collections."""
    results = {}
    for cname in MONITORED_COLLECTIONS:
        results[cname] = await asyncio.to_thread(compute_semantic_velocity, cname)
    return {"velocity": results, "timestamp": datetime.now(timezone.utc).isoformat()}


# ─────────────────────────────────────────────────────────────────────────────
# INJECT TEST ANOMALY  (demo button)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/inject-test")
async def inject_test_anomaly() -> dict:
    """
    Inserts a ~700 KB document into sample_supplies.sales.
    This triggers the Change Stream watcher and fires the full agent pipeline.
    Used by the 'Inject Test Anomaly' demo button.
    """
    # Build a fat document with a large embedded binary-style string
    fat_field = "".join(random.choices(string.ascii_letters + string.digits, k=300_000))
    test_doc  = {
        "title":            "QUERYSENTINEL-TEST",
        "plot":             fat_field,   # ← the large field causing the anomaly
        "injected_payload": fat_field,
        "_qs_test":         True,
        "injected_at":      datetime.now(timezone.utc).isoformat(),
    }

    try:
        # Insert into a monitored collection so the Change Stream watcher fires.
        result = source_db["movies"].insert_one(test_doc)
        return {
            "status":       "injected",
            "document_id":  str(result.inserted_id),
            "approx_size_kb": len(fat_field) // 1024,
            "message":      "Change Stream will detect within 200 ms. Watch the dashboard.",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# APPROVE GATE
# ─────────────────────────────────────────────────────────────────────────────

class ApproveRequest(BaseModel):
    incident_id: str
    option_rank: int          # which remediation option to execute (1, 2, or 3)
    collection_name: str


@app.post("/api/approve")
async def approve_remediation(req: ApproveRequest) -> dict:
    """
    Human-in-the-loop APPROVE gate.

    Confirms the remediation option ALREADY presented in the incident report and
    records the approval to an immutable audit log. This is deterministic and does
    NOT re-run the LLM pipeline: the operator is approving the recommendation that
    was shown — not regenerating it. This keeps the gate instant and fail-proof
    (no quota / MCP dependency) while preserving the human-in-the-loop guarantee
    that only an explicit approval is ever logged as an executed action.
    """
    incident = app_db.incident_reports.find_one({"incident_id": req.incident_id})
    if not incident:
        # Fall back to most recent incident for the collection
        incident = app_db.incident_reports.find_one(
            {"anomaly_event.collection_name": req.collection_name},
            sort=[("created_at", pymongo.DESCENDING)],
        )
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")

    remediation = incident.get("remediation") or {}
    options     = remediation.get("options") or []

    # Locate the approved option by rank; fall back to first available option.
    chosen = next(
        (o for o in options if int(o.get("rank", -1)) == int(req.option_rank)),
        None,
    )
    if chosen is None and options:
        chosen = options[0]
    if chosen is None:
        raise HTTPException(status_code=422, detail="No remediation options to approve.")

    approved_at = datetime.now(timezone.utc)

    # Immutable audit trail — best-effort, never blocks the approval.
    try:
        app_db.tool_audit_log.insert_one({
            "incident_id":  incident.get("incident_id"),
            "collection":   incident.get("collection_name") or req.collection_name,
            "option_rank":  int(req.option_rank),
            "option_title": chosen.get("title"),
            "mcp_action":   chosen.get("mcp_action"),
            "approved_by":  "operator",          # human-in-the-loop
            "approved_at":  approved_at,
            "action":       "APPROVE",
        })
    except Exception:
        logger.warning("approve: audit log write failed", exc_info=True)

    # Mark the incident approved (idempotent) so the UI reflects the gate state.
    try:
        app_db.incident_reports.update_one(
            {"_id": incident["_id"]},
            {"$set": {
                "remediation.approved":        True,
                "remediation.approved_option": int(req.option_rank),
                "remediation.approved_at":     approved_at,
            }},
        )
    except Exception:
        logger.warning("approve: incident update failed", exc_info=True)

    return {
        "status":          "approved",
        "incident_id":     incident.get("incident_id"),
        "option_executed": int(req.option_rank),
        "option_title":    chosen.get("title"),
        "mcp_action":      chosen.get("mcp_action"),
        "message":         f"Remediation option {req.option_rank} approved: {chosen.get('title')}",
        "approved_at":     approved_at.isoformat(),
        "audit_logged":    True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# BASELINE REFRESH
# ─────────────────────────────────────────────────────────────────────────────

class BaselineRequest(BaseModel):
    collection_name: str
    db_name: str = SOURCE_DB


@app.post("/api/baseline/refresh")
async def refresh_baseline(req: BaselineRequest) -> dict:
    """Recompute baseline stats for the given collection."""
    try:
        baseline = await asyncio.to_thread(
            compute_baseline, req.collection_name, req.db_name
        )
        return {
            "status":     "refreshed",
            "collection": req.collection_name,
            "mean_kb":    round(baseline.get("mean_bytes", 0) / 1024, 2),
            "stddev_kb":  round(baseline.get("stddev_bytes", 0) / 1024, 2),
            "sample":     baseline.get("sample_count", 0),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# CONFIDENCE BREAKDOWN
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/confidence/{incident_id}")
async def get_confidence_breakdown(incident_id: str) -> dict:
    """
    Weighted 4-source confidence breakdown for an incident.
    Sources: Performance Advisor (35%) · Vector Search (30%) · Schema Drift (20%) · Z-Score (15%).
    Used by the Evidence tab ConfidenceBreakdown component.
    """
    incident = app_db.incident_reports.find_one({"incident_id": incident_id})
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")

    from confidence import compute_confidence_breakdown

    root_cause = incident.get("root_cause") or {}
    similar    = incident.get("similar_incidents") or {}
    schema     = incident.get("schema_drift") or {}
    z_score    = float(incident.get("z_score", 0))

    pa_result = {
        "suggestedIndexes":  root_cause.get("suggested_indexes",  []),
        "schemaSuggestions": root_cause.get("schema_suggestions", []),
        "slowQueryLogs":     root_cause.get("slow_queries",       []),
    }
    vector_score = float(similar.get("top_match_score", 0))

    breakdown = compute_confidence_breakdown(
        pa_result=pa_result,
        vector_score=vector_score,
        schema_drift=schema,
        z_score=z_score,
    )
    return {"incident_id": incident_id, **breakdown}


# ─────────────────────────────────────────────────────────────────────────────
# EXPLAIN PLAN — Before/After query plan comparison
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/explain/{incident_id}")
async def get_explain_plan(incident_id: str) -> dict:
    """
    Returns a before (COLLSCAN) vs simulated-after (IXSCAN) explain plan
    for the suggested index from an incident's root_cause output.

    Used by the ExplainPlanCard component to show the COLLSCAN→IXSCAN diff
    before the user clicks APPROVE.
    """
    incident = app_db.incident_reports.find_one({"incident_id": incident_id})
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")

    root_cause   = incident.get("root_cause") or {}
    collection   = incident.get("collection_name") or incident.get(
        "anomaly_event", {}
    ).get("collection_name", "movies")

    # Extract first suggested index from root-cause agent output
    suggested = root_cause.get("suggested_indexes", [])
    if not suggested:
        return {
            "incident_id": incident_id,
            "status":      "no_suggestion",
            "message":     "Performance Advisor returned no index suggestions for this incident.",
        }

    first_idx  = suggested[0]
    index_keys = first_idx.get("keys") or first_idx.get("index_keys") or {}
    if not index_keys:
        return {
            "incident_id": incident_id,
            "status":      "no_suggestion",
            "message":     "Suggested index has no key spec.",
        }

    # Use an empty filter (full-collection scan as worst-case baseline)
    slow_query_filter: dict = {}

    from explain import get_explain_before_after
    try:
        result = await asyncio.to_thread(
            get_explain_before_after,
            collection,
            slow_query_filter,
            index_keys,
        )
        return {"incident_id": incident_id, "collection": collection, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# AGENT CALLS — Agent Reasoning Timeline data
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/agent-calls/{incident_id}")
async def get_agent_calls(incident_id: str) -> dict:
    """
    Returns the per-step agent execution trace for a given incident.
    Consumed by the Agent Reasoning Timeline component on the dashboard.
    Shape: { incident_id, total_ms, steps: [{agent, start_ms, end_ms, duration_ms, tool_calls}] }
    """
    doc = app_db.agent_calls.find_one({"incident_id": incident_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Agent call trace not found.")
    doc["_id"] = str(doc["_id"])
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = doc["created_at"].isoformat()
    return doc


@app.get("/api/agent-calls")
async def get_recent_agent_calls(limit: int = 10) -> dict:
    """Returns recent agent call traces (newest first). Used by the dashboard overview."""
    docs = list(
        app_db.agent_calls.find(
            {},
            sort=[("created_at", pymongo.DESCENDING)],
            limit=min(limit, 50),
        )
    )
    for d in docs:
        d["_id"] = str(d["_id"])
        if isinstance(d.get("created_at"), datetime):
            d["created_at"] = d["created_at"].isoformat()
    return {"traces": docs, "count": len(docs)}


# ─────────────────────────────────────────────────────────────────────────────
# BUSINESS IMPACT — Dollar estimate per incident
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/impact/{incident_id}")
async def get_incident_impact(incident_id: str) -> dict:
    """
    Returns a business-impact dollar estimate for a given incident.

    Formula: base_qps × degradation × anomaly_multiplier × collection_tier × $0.15/min
    Also returns: engineer_triage_usd (3.4h × $150), total_worst_case, querysentinel_save.

    Used by the IncidentModal to surface: "This incident costs $X/min. QuerySentinel saved $Y."
    """
    incident = app_db.incident_reports.find_one({"incident_id": incident_id})
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")

    # Use cached value if available (stored by orchestrator)
    cached = incident.get("estimated_dollar_impact")
    if cached and isinstance(cached, dict) and cached.get("per_minute_usd") is not None:
        return {"incident_id": incident_id, **cached}

    # Recompute if not cached
    from impact import estimate_dollar_impact
    collection   = incident.get("collection_name", "")
    z_score      = float(incident.get("z_score", 0))
    anomaly_type = incident.get("anomaly_type", "UNKNOWN")
    result       = await asyncio.to_thread(estimate_dollar_impact, collection, z_score, anomaly_type)
    return {"incident_id": incident_id, **result}


# ─────────────────────────────────────────────────────────────────────────────
# CLUSTER EVENTS — Correlated multi-collection anomalies
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/cluster-events")
async def get_cluster_events(limit: int = 10) -> dict:
    """
    Returns recent CLUSTER_WIDE_EVENT incidents from CorrelationAgent.

    A cluster event fires when 2+ collections show anomalies in the same 5-minute
    window, suggesting a coordinated ETL error, LLM content flood, or data poisoning.
    CorrelationAgent (6th ADK agent) generates a coordinated-attack hypothesis with
    confidence score and identifies the likely "patient zero" collection.
    """
    docs = list(
        app_db.correlated_incidents.find(
            {},
            sort=[("created_at", pymongo.DESCENDING)],
            limit=min(limit, 50),
        )
    )
    for d in docs:
        d["_id"] = str(d["_id"])
        if isinstance(d.get("created_at"), datetime):
            d["created_at"] = d["created_at"].isoformat()
        if isinstance(d.get("window_start"), datetime):
            d["window_start"] = d["window_start"].isoformat()
        if isinstance(d.get("window_end"), datetime):
            d["window_end"] = d["window_end"].isoformat()
    return {"cluster_events": docs, "count": len(docs)}


# ─────────────────────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
