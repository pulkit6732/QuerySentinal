"""
correlation.py — CorrelationAgent: coordinated multi-collection anomaly detector.

Architecture:
  After each incident pipeline completes, the watcher calls run_correlation_check().
  CorrelationAgent looks at raw_events from the last 5 minutes across ALL monitored
  collections. If 2+ collections fired anomalies in the same window, it:

    1. Builds a coordinated-attack hypothesis (LLM-generated, not rule-based)
    2. Identifies the likely "patient zero" collection
    3. Rates correlation confidence (0.0–1.0)
    4. Creates a CLUSTER_WIDE_EVENT meta-incident in app_db.correlated_incidents
    5. Promotes severity to CRITICAL if confidence > 0.7

This is the "coordinated ingestion attack" detector. No existing tool has this.
It is also the 6th ADK agent in the system, demonstrating that QUERYSENTINEL's
agent architecture is extensible — new detection capabilities drop in as agents.

Rate limiting: one CLUSTER_WIDE_EVENT per window per collection-set per hour.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

from config import GEMINI_MODEL, MONITORED_COLLECTIONS
from db import app_db

logger = logging.getLogger("correlation")

_CORRELATION_WINDOW_MIN = 5          # look back N minutes
_MIN_COLLECTIONS        = 2          # minimum collections to constitute a cluster event
_REFIRE_INTERVAL        = timedelta(hours=1)   # rate-limit: one cluster incident per hour per set

_session_service = InMemorySessionService()

_INSTRUCTION = """You are CorrelationAgent for QUERYSENTINEL.

You receive a JSON payload describing anomalies from multiple MongoDB collections
that ALL fired within the same 5-minute window. Your job is coordinated-attack detection.

Analyze whether these anomalies share a common root cause. Common patterns:
- Misconfigured ETL pipeline writing to multiple collections simultaneously
- Coordinated LLM-generated content flood (same embedding centroid drift across collections)
- Infrastructure-level event: network partition, replication lag, Ops Manager misconfiguration
- Intentional data poisoning attack targeting multiple collections

Respond ONLY with this exact JSON (no markdown fences, no extra keys):
{
  "is_coordinated": true or false,
  "correlation_confidence": 0.0-1.0,
  "hypothesis": "One sentence describing the likely common cause",
  "patient_zero": "collection_name that likely received the contamination first, or null",
  "attack_pattern": "ETL_MISCONFIGURATION | LLM_FLOOD | INFRASTRUCTURE | DATA_POISONING | UNKNOWN",
  "collections_affected": ["list", "of", "names"],
  "recommended_action": "Concrete 1-sentence action for the on-call engineer",
  "cluster_severity": "CRITICAL or WARNING"
}

If the anomalies appear unrelated (different anomaly types, different Z-score magnitudes,
staggered timing suggesting independent causes), set is_coordinated to false.
"""


def build_correlation_agent() -> LlmAgent:
    """Build a fresh CorrelationAgent LlmAgent (no MCP tools needed — reads session state)."""
    return LlmAgent(
        name="CorrelationAgent",
        model=GEMINI_MODEL,
        instruction=_INSTRUCTION,
        output_key="correlation_result",
    )


async def run_correlation_check(trigger_collection: str) -> Optional[dict]:
    """
    Check if a recent anomaly on trigger_collection is part of a cluster event.

    Called from watcher.py after run_incident_pipeline() returns.
    Returns a cluster incident dict if a coordinated anomaly is found, None otherwise.

    Args:
        trigger_collection: The collection that just fired an incident

    Returns:
        Cluster incident dict (stored in app_db.correlated_incidents) or None
    """
    window_start = datetime.now(timezone.utc) - timedelta(minutes=_CORRELATION_WINDOW_MIN)

    # ── 1. Fetch recent anomalies across all monitored collections ────────────
    recent = list(app_db.raw_events.find(
        {
            "detected_at":      {"$gte": window_start},
            "collection_name":  {"$in": MONITORED_COLLECTIONS},
        },
        {
            "collection_name": 1, "anomaly_type": 1,
            "z_score": 1, "detected_at": 1, "_id": 0,
        },
        sort=[("detected_at", -1)],
        limit=100,
    ))

    if not recent:
        return None

    # ── 2. Group by collection ────────────────────────────────────────────────
    by_collection: dict[str, list] = {}
    for evt in recent:
        cname = evt.get("collection_name", "unknown")
        by_collection.setdefault(cname, []).append(evt)

    affected = sorted(by_collection.keys())
    if len(affected) < _MIN_COLLECTIONS:
        return None   # single-collection event — not a cluster incident

    # ── 3. Rate-limit: don't re-fire within 1h for the same collection set ───
    key = "|".join(affected)
    existing = app_db.correlated_incidents.find_one({
        "collection_key": key,
        "created_at":     {"$gte": datetime.now(timezone.utc) - _REFIRE_INTERVAL},
    })
    if existing:
        logger.debug("Correlation suppressed (rate-limited): %s", key)
        return None

    # ── 4. Build payload for CorrelationAgent ─────────────────────────────────
    correlation_id = str(uuid.uuid4())
    payload = {
        "trigger_collection": trigger_collection,
        "window_minutes":     _CORRELATION_WINDOW_MIN,
        "anomaly_groups": {
            cname: [
                {
                    "anomaly_type": e.get("anomaly_type"),
                    "z_score":      round(e.get("z_score", 0), 2),
                    "detected_at":  (
                        e["detected_at"].isoformat()
                        if isinstance(e.get("detected_at"), datetime)
                        else str(e.get("detected_at", ""))
                    ),
                }
                for e in evts
            ]
            for cname, evts in by_collection.items()
        },
    }

    # ── 5. Run CorrelationAgent ───────────────────────────────────────────────
    try:
        agent  = build_correlation_agent()
        runner = Runner(
            agent=agent,
            app_name="querysentinel-correlation",
            session_service=_session_service,
        )
        await _session_service.create_session(
            app_name="querysentinel-correlation",
            user_id=correlation_id,
            session_id=correlation_id,
        )
        async for event in runner.run_async(
            user_id=correlation_id,
            session_id=correlation_id,
            new_message=Content(
                role="user",
                parts=[Part.from_text(text=json.dumps(payload, default=str))],
            ),
        ):
            if event.is_final_response():
                break
    except Exception as e:
        logger.error("CorrelationAgent run failed: %s", e)
        return None

    # ── 6. Parse agent output ─────────────────────────────────────────────────
    session = await _session_service.get_session(
        app_name="querysentinel-correlation",
        user_id=correlation_id,
        session_id=correlation_id,
    )
    state = getattr(session, "state", {}) or {}
    raw   = state.get("correlation_result", "{}")

    try:
        if isinstance(raw, str):
            clean = raw.strip()
            if clean.startswith("```"):
                lines = clean.split("\n")
                clean = "\n".join(
                    lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
                ).strip()
            result = json.loads(clean)
        else:
            result = raw if isinstance(raw, dict) else {}
    except Exception as e:
        logger.warning("CorrelationAgent output parse failed: %s | raw=%s", e, raw)
        result = {"parse_error": True, "raw": raw}

    if not result.get("is_coordinated"):
        return None   # Agent determined anomalies are independent

    # ── 7. Build and persist cluster incident ─────────────────────────────────
    confidence = float(result.get("correlation_confidence", 0.0))
    severity   = result.get("cluster_severity", "WARNING")
    if confidence >= 0.7:
        severity = "CRITICAL"   # override to CRITICAL if high-confidence correlation

    cluster_incident = {
        "correlation_id":       correlation_id,
        "incident_type":        "CLUSTER_WIDE_EVENT",
        "trigger_collection":   trigger_collection,
        "collection_key":       key,
        "collections_affected": affected,
        "window_start":         window_start,
        "window_end":           datetime.now(timezone.utc),
        "correlation_result":   result,
        "severity":             severity,
        "hypothesis":           result.get("hypothesis", ""),
        "attack_pattern":       result.get("attack_pattern", "UNKNOWN"),
        "patient_zero":         result.get("patient_zero"),
        "confidence":           round(confidence, 3),
        "recommended_action":   result.get("recommended_action", ""),
        "created_at":           datetime.now(timezone.utc),
    }

    try:
        app_db.correlated_incidents.insert_one(cluster_incident.copy())
        logger.info(
            "CLUSTER_WIDE_EVENT [%s] %s — %s (confidence=%.2f, collections=%s)",
            severity,
            result.get("attack_pattern", "?"),
            result.get("hypothesis", "?")[:80],
            confidence,
            affected,
        )
    except Exception as e:
        logger.warning("Could not persist cluster incident: %s", e)

    return cluster_incident
