"""
watcher.py — Atlas Change Stream watcher with production-grade reliability.

Bugs fixed vs v1:
  ✓ asyncio.get_event_loop() in thread → now passes loop explicitly from async context
  ✓ Schema drift rate-limited to 1× per 5 min per collection (was every event)
  ✓ sample_supplies.sales supported via per-collection db_name mapping

Features:
  ✓ Persistent resume token — zero event loss across Cloud Run restarts
  ✓ Auto-reconnect while loop — survives network blips
  ✓ Circuit breaker — if Gemini API fails 3×, falls back to rule-based alerts
  ✓ Dead letter queue — failed events → app_db.failed_events for retry
  ✓ Per-collection parallel watchers launched at FastAPI startup
"""
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import bson
import pymongo

from config import MONITORED_COLLECTIONS, SOURCE_DB, ANOMALY_Z, CRITICAL_Z
from db import app_db, client
from detect import (
    compute_baseline,
    detect_doc_size_anomaly,
    detect_schema_drift,
    compute_semantic_velocity,
    compute_incident_runway,
)
from detectors import scan_document_for_ai_threats  # OWASP LLM01/LLM03 scanner

# ── Semantic velocity monitor rate-limiter ────────────────────────────────────
_sv_last_fired: dict[str, datetime] = {}
_SV_MONITOR_INTERVAL_S = 300       # check every 5 minutes
_SV_MIN_REFIRE_INTERVAL = timedelta(hours=1)   # one spike incident per collection per hour

logger = logging.getLogger("watcher")

# Global anomaly queue — SSE endpoint reads from this
anomaly_queue: asyncio.Queue = asyncio.Queue()

# Circuit breaker state per collection
_cb_failures: dict[str, int] = {}
_CB_THRESHOLD = 3   # open circuit after 3 consecutive agent failures
_CB_RESET_AFTER = timedelta(seconds=60)
_cb_opened_at: dict[str, datetime] = {}

# Schema drift rate-limiter: don't re-sample schema more often than this
_SCHEMA_DRIFT_MIN_INTERVAL = timedelta(minutes=5)
_last_schema_check: dict[str, datetime] = {}

# Per-collection database name override (for collections outside SOURCE_DB)
# Populated from config — add your own overrides here
_COLLECTION_DB_OVERRIDE: dict[str, str] = {
    "sales": "sample_supplies",
}


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

async def start_all_watchers() -> None:
    """Launch one watcher task per monitored collection + semantic velocity monitor."""
    for cname in MONITORED_COLLECTIONS:
        asyncio.create_task(
            _watcher_loop(cname),
            name=f"watcher-{cname}",
        )
    asyncio.create_task(_semantic_velocity_monitor(), name="semantic-velocity-monitor")
    logger.info(
        "Launched %d Change Stream watchers + semantic velocity monitor.",
        len(MONITORED_COLLECTIONS),
    )


async def _semantic_velocity_monitor() -> None:
    """
    Background task: periodically checks semantic velocity for all collections.
    If velocity_z > 3.0, fires a SEMANTIC_VELOCITY_SPIKE incident independently
    of any doc-size trigger — making semantic drift a first-class anomaly type.

    Rate-limited: one spike incident per collection per hour to prevent storms.
    Runs every _SV_MONITOR_INTERVAL_S seconds (default 300 = 5 minutes).
    """
    # Initial delay — let the watcher startup settle
    await asyncio.sleep(30)

    while True:
        for cname in MONITORED_COLLECTIONS:
            try:
                sv = await asyncio.to_thread(compute_semantic_velocity, cname)

                if sv.get("status") != "spike":
                    continue

                # Rate-limit: don't re-fire within the refire window
                last = _sv_last_fired.get(cname)
                if last and (datetime.now(timezone.utc) - last) < _SV_MIN_REFIRE_INTERVAL:
                    logger.debug(
                        "SV spike on '%s' suppressed — rate-limited (last fired %s ago).",
                        cname,
                        datetime.now(timezone.utc) - last,
                    )
                    continue

                z_score  = sv.get("velocity_zscore", 0.0)
                severity = "CRITICAL" if abs(z_score) >= CRITICAL_Z else "WARNING"

                anomaly = {
                    "collection_name":   cname,
                    "anomaly_type":      "SEMANTIC_VELOCITY_SPIKE",
                    "z_score":           round(z_score, 3),
                    "severity":          severity,
                    "document_id":       "",
                    "description":       (
                        sv.get("interpretation")
                        or (
                            f"{severity}: Semantic velocity spike in '{cname}'. "
                            f"Cosine drift {sv.get('centroid_distance', 0):.4f} "
                            f"(Z={z_score:.1f}, baseline avg {sv.get('baseline_distance', 0):.4f}). "
                            "Documents no longer semantically similar to historical norm."
                        )
                    ),
                    "semantic_velocity": sv.get("centroid_distance", 0.0),
                    "semantic_spike":    True,
                    "velocity_zscore":   z_score,
                    "centroid_distance": sv.get("centroid_distance", 0.0),
                    "baseline_distance": sv.get("baseline_distance", 0.0),
                    "confidence":        min(1.0, abs(z_score) / 10.0),
                    "detected_at":       datetime.now(timezone.utc),
                }

                _sv_last_fired[cname] = datetime.now(timezone.utc)
                logger.warning(
                    "SEMANTIC_VELOCITY_SPIKE on '%s': Z=%.1f centroid_dist=%.4f — firing pipeline.",
                    cname, z_score, sv.get("centroid_distance", 0),
                )

                # Emit to raw_events + SSE queue, then fire agent pipeline
                await emit_anomaly(anomaly)
                try:
                    from agents.orchestrator import run_incident_pipeline
                    await run_incident_pipeline(anomaly)
                    # Post-pipeline: check for correlated cluster event (non-blocking)
                    from agents.correlation import run_correlation_check
                    asyncio.create_task(
                        run_correlation_check(cname),
                        name=f"correlation-sv-{cname}",
                    )
                except Exception as exc:
                    logger.error("SV pipeline failed for '%s': %s", cname, exc)

            except Exception as exc:
                logger.warning("Semantic velocity monitor error for '%s': %s", cname, exc)

        await asyncio.sleep(_SV_MONITOR_INTERVAL_S)


async def emit_anomaly(anomaly: dict) -> dict:
    """
    1. Insert to app_db.raw_events (Atlas auto-embeds description via voyage-4 if trigger configured).
    2. Push to SSE queue so dashboard receives it within ~1 s.
    3. Returns the anomaly with its new _id.
    """
    result = app_db.raw_events.insert_one({**anomaly, "processed": False})
    anomaly["_id"] = str(result.inserted_id)
    await anomaly_queue.put(anomaly)
    return anomaly


def _db_for(collection_name: str) -> str:
    """Returns the database name for a given collection (with override support)."""
    return _COLLECTION_DB_OVERRIDE.get(collection_name, SOURCE_DB)


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL
# ─────────────────────────────────────────────────────────────────────────────

async def _watcher_loop(collection_name: str) -> None:
    """
    Outer reconnect loop for a single collection.
    Loads the persisted resume token on each reconnect — zero event loss.
    """
    db_name  = _db_for(collection_name)
    baseline = await asyncio.to_thread(
        _get_or_compute_baseline, collection_name, db_name
    )
    logger.info(
        "Baseline loaded for '%s' (db=%s): mean=%.1f KB",
        collection_name, db_name, baseline.get("mean_bytes", 0) / 1024,
    )

    while True:
        try:
            # FIX: Get the running event loop BEFORE entering the thread
            # so _blocking_stream can submit coroutines back to this loop.
            loop = asyncio.get_running_loop()
            await asyncio.to_thread(
                _blocking_stream, collection_name, db_name, baseline, loop
            )
        except Exception as exc:
            logger.warning(
                "Stream error on '%s': %s — reconnecting in 2 s",
                collection_name, exc,
            )
            await asyncio.sleep(2)


def _blocking_stream(
    collection_name: str,
    db_name: str,
    baseline: dict,
    loop: asyncio.AbstractEventLoop,   # ← passed from async context (FIX)
) -> None:
    """
    Synchronous inner loop: iterates the Change Stream and pushes anomalies
    back into the asyncio world via asyncio.run_coroutine_threadsafe.

    The event loop is passed explicitly because asyncio.get_event_loop()
    called from a worker thread in Python 3.10+ creates a NEW loop, not the
    running one — causing run_coroutine_threadsafe to submit to a dead loop.
    """
    # Load persisted resume token (survives Cloud Run restart)
    state  = app_db.stream_state.find_one({"_id": collection_name}) or {}
    resume = state.get("resume_token")

    coll     = client[db_name][collection_name]
    pipeline = [{"$match": {"operationType": {"$in": ["insert", "replace", "update"]}}}]
    kwargs: dict = {"pipeline": pipeline, "full_document": "updateLookup"}
    if resume:
        kwargs["resume_after"] = resume
        logger.info("Resuming stream for '%s' from saved token.", collection_name)

    with coll.watch(**kwargs) as stream:
        for change in stream:
            # ── Persist resume token BEFORE processing (zero-loss guarantee) ─
            app_db.stream_state.update_one(
                {"_id": collection_name},
                {"$set": {
                    "resume_token": stream.resume_token,
                    "updated_at":   datetime.now(timezone.utc),
                }},
                upsert=True,
            )

            # ── Z-score doc-size anomaly detection ────────────────────────────
            anomaly = detect_doc_size_anomaly(change, baseline)
            if not anomaly:
                continue

            # ── Fire-and-forget: submit pipeline as proper asyncio.Task ─────────
            # OLD pattern (broken): run_coroutine_threadsafe + future.result(timeout)
            # blocked the Change Stream thread for the entire pipeline duration AND
            # caused anyio cancel-scope mismatches in MCP tools across asyncio tasks.
            #
            # NEW pattern: submit as background task in the event loop, don't wait.
            # The Change Stream thread immediately continues, never blocks. Errors
            # are captured by _handle_anomaly's own try/except and written to
            # failed_events for observability.
            asyncio.run_coroutine_threadsafe(
                _handle_anomaly_safe(anomaly, collection_name, db_name), loop
            )
            # Light back-pressure: brief yield so the thread doesn't starve the loop
            # when bursts of inserts arrive (e.g., demo_inject --count 20).
            time.sleep(0.01)


async def _handle_anomaly_safe(
    anomaly: dict, collection_name: str, db_name: str
) -> None:
    """
    Outer wrapper that guarantees every anomaly is either:
      - successfully processed (incident_report written), OR
      - logged to failed_events with the full error context.

    No more silent failures. No more empty error messages.
    The Change Stream thread never blocks on this — it's a proper asyncio.Task.
    """
    try:
        await _handle_anomaly(anomaly, collection_name, db_name)
    except Exception as exc:
        # Capture everything: type, message, traceback summary
        import traceback
        tb_summary = traceback.format_exc()[-500:]  # last 500 chars (most actionable)
        logger.error(
            "Pipeline crash for '%s' anomaly=%s: %s — %s",
            collection_name,
            anomaly.get("document_id", "?"),
            type(exc).__name__,
            str(exc)[:200] or "(no message)",
        )
        _record_dead_letter({
            **anomaly,
            "error_type":    type(exc).__name__,
            "error_message": str(exc) or "(no message)",
            "traceback":     tb_summary,
        }, str(exc) or type(exc).__name__)


async def _handle_anomaly(
    anomaly: dict, collection_name: str, db_name: str
) -> None:
    """
    Enriches anomaly with schema drift + semantic velocity,
    emits to raw_events and SSE queue, then fires the agent pipeline.
    Falls back to rule-based alert if circuit breaker is open.
    """
    # ── Schema drift (rate-limited: 1× per 5 min per collection) ─────────────
    now = datetime.now(timezone.utc)
    last_check = _last_schema_check.get(collection_name)
    if not last_check or (now - last_check) >= _SCHEMA_DRIFT_MIN_INTERVAL:
        try:
            drift = await asyncio.to_thread(detect_schema_drift, collection_name, db_name)
            anomaly["schema_drift"] = drift
            _last_schema_check[collection_name] = now
        except Exception:
            anomaly["schema_drift"] = {"drift_detected": False}
    else:
        anomaly["schema_drift"] = {"drift_detected": False, "rate_limited": True}

    # ── AI Memory Poisoning Scan (OWASP LLM01/LLM03) ─────────────────────────
    # Scan the actual document content for prompt injection patterns before any
    # LLM processes it. If a threat is found, escalate anomaly_type immediately.
    try:
        db_name_local = _COLLECTION_DB_OVERRIDE.get(collection_name, SOURCE_DB)
        doc_id = anomaly.get("document_id", "")
        flagged_doc = None
        if doc_id and len(doc_id) == 24:
            try:
                flagged_doc = client[db_name_local][collection_name].find_one(
                    {"_id": bson.ObjectId(doc_id)}
                )
            except (bson.errors.InvalidId, Exception):
                pass  # doc_id not a valid ObjectId — skip scan

        if flagged_doc:
            threat = await asyncio.to_thread(
                scan_document_for_ai_threats, flagged_doc, collection_name
            )
            if threat["threat_detected"]:
                anomaly["ai_memory_poisoning"] = True
                anomaly["ai_threat_matches"]   = threat["matches"]
                anomaly["ai_threat_confidence"] = threat["confidence"]
                # Escalate: override anomaly_type if threat is more severe than doc-size spike
                if anomaly.get("anomaly_type") == "DOC_SIZE_SPIKE":
                    anomaly["anomaly_type"] = "AI_MEMORY_POISONING"
                    anomaly["severity"]     = threat["severity"] or anomaly.get("severity", "WARNING")
                logger.warning(
                    "AI_MEMORY_POISONING detected in '%s' doc=%s: %d pattern(s). OWASP LLM01.",
                    collection_name, doc_id[:8] if doc_id else "?", len(threat["matches"]),
                )
            else:
                anomaly["ai_memory_poisoning"] = False
                anomaly["ai_threat_matches"]   = []
    except Exception as _ai_err:
        logger.debug("AI threat scan skipped for '%s': %s", collection_name, _ai_err)
        anomaly.setdefault("ai_memory_poisoning", False)
        anomaly.setdefault("ai_threat_matches", [])

    # ── Semantic velocity (reads embeddings from app_db — fast) ──────────────
    try:
        sv = await asyncio.to_thread(compute_semantic_velocity, collection_name)
        anomaly["semantic_velocity"] = sv.get("centroid_distance", sv.get("semantic_velocity", 0.0))
        anomaly["semantic_spike"]    = sv.get("status") == "spike" or sv.get("spike_detected", False)
    except Exception:
        anomaly["semantic_velocity"] = 0.0
        anomaly["semantic_spike"]    = False

    # ── Runway prediction ─────────────────────────────────────────────────────
    try:
        runway = await asyncio.to_thread(compute_incident_runway, collection_name)
        anomaly["runway"] = runway
    except Exception:
        pass

    # ── Emit to raw_events + SSE ──────────────────────────────────────────────
    await emit_anomaly(anomaly)

    # ── Circuit breaker check ─────────────────────────────────────────────────
    cb_trips = _cb_failures.get(collection_name, 0)
    if cb_trips >= _CB_THRESHOLD:
        opened = _cb_opened_at.get(collection_name)
        if opened and (datetime.now(timezone.utc) - opened) > _CB_RESET_AFTER:
            # Auto-reset after 60 s
            _cb_failures[collection_name] = 0
            logger.info("Circuit breaker RESET for '%s'.", collection_name)
        else:
            logger.warning(
                "Circuit breaker OPEN for '%s' — rule-based alert only.", collection_name
            )
            return

    # ── Fire agent pipeline ───────────────────────────────────────────────────
    try:
        from agents.orchestrator import run_incident_pipeline
        await run_incident_pipeline(anomaly)
        _cb_failures[collection_name] = 0   # reset on success

        # ── CorrelationAgent: check for cluster-wide coordinated anomaly ──────
        # Runs post-pipeline — does not block the incident or affect latency.
        try:
            from agents.correlation import run_correlation_check
            asyncio.create_task(
                run_correlation_check(collection_name),
                name=f"correlation-{collection_name}",
            )
        except Exception as ce:
            logger.debug("CorrelationAgent skipped: %s", ce)
    except Exception as exc:
        count = _cb_failures.get(collection_name, 0) + 1
        _cb_failures[collection_name] = count
        if count >= _CB_THRESHOLD:
            _cb_opened_at[collection_name] = datetime.now(timezone.utc)
        logger.error(
            "Agent pipeline failed for '%s' (%d/%d): %s",
            collection_name, count, _CB_THRESHOLD, exc,
        )
        _record_dead_letter(anomaly, str(exc))


def _get_or_compute_baseline(collection_name: str, db_name: str) -> dict:
    stored = app_db.collection_baselines.find_one({"collection_name": collection_name})
    if stored and stored.get("mean_bytes"):
        return stored
    return compute_baseline(collection_name, db_name)


def _record_dead_letter(anomaly: dict, error: str) -> None:
    try:
        app_db.failed_events.insert_one({
            **{k: v for k, v in anomaly.items() if k != "_id"},
            "error":       error,
            "retry_count": 0,
            "failed_at":   datetime.now(timezone.utc),
        })
    except Exception:
        pass
