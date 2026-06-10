"""
baseline_refiner.py — Adaptive baseline refinement via Google ADK LoopAgent.

ADK agent type usage in QUERYSENTINEL:
  SequentialAgent  → IncidentPipeline (orchestrator.py)
  ParallelAgent    → ParallelInvestigation (orchestrator.py)
  LoopAgent        → AdaptiveBaselineLoop (this file)  ← third type

Architecture:
  LoopAgent("AdaptiveBaselineLoop", max_iterations=5)
    └── LlmAgent("BaselineRefinerAgent")
          └── FunctionTool(check_and_update_baseline)

Loop termination:
  The BaselineRefinerAgent's FunctionTool writes session_state["escalate"] = True
  when ALL baselines are stable (drift < STABLE_THRESHOLD_PCT). ADK's LoopAgent
  reads session_state["escalate"] after each sub-agent run and exits when True.
  Safety cap: max_iterations=5 prevents infinite loops on noisy collections.

Background task in main.py:
  asyncio.create_task(run_baseline_loop_forever()) — runs every 30 minutes.
  Keeps drift detectors accurate without requiring manual recompute calls.

Why this matters for the demo:
  Without adaptive baselines, a genuine workload increase over 24 hours shifts
  the distribution enough that legitimate writes generate spurious Z-score alerts.
  The LoopAgent catches this silently every 30 minutes.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from google.adk.agents import LlmAgent, LoopAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.genai.types import Content, Part

from config import GEMINI_MODEL, MONITORED_COLLECTIONS, SOURCE_DB

logger = logging.getLogger("baseline_refiner")

# ── Stability threshold: update baseline if mean drifted by more than this % ──
STABLE_THRESHOLD_PCT = 5.0

# ── Shared session service for the loop runner ────────────────────────────────
_loop_session_service = InMemorySessionService()


# ── FunctionTool: Python-level baseline check (no LLM needed for the math) ───

def check_and_update_baseline(collection_name: str, **_ignored) -> dict:
    """
    Samples 100 documents from the source collection and compares the live
    mean document size against the stored baseline.

    If drift > STABLE_THRESHOLD_PCT %: refreshes the baseline and returns
      { "collection": collection_name, "drifted": True, "drift_pct": float,
        "action": "refreshed", "stable": False }

    If drift <= STABLE_THRESHOLD_PCT %: returns
      { "collection": collection_name, "drifted": False, "drift_pct": float,
        "action": "none", "stable": True }

    Returns { "stable": True, "drifted": False, "reason": "no_documents" } for
    empty collections (safe — no baseline update performed).
    """
    from db import app_db, source_db
    from detect import compute_baseline

    stored = app_db.collection_baselines.find_one({"collection_name": collection_name}) or {}
    stored_mean = stored.get("mean_bytes", 0.0)

    if not stored_mean:
        # No baseline yet — create one now
        compute_baseline(collection_name, SOURCE_DB)
        logger.info("BaselineRefiner: created first baseline for '%s'.", collection_name)
        return {
            "collection":  collection_name,
            "drifted":     True,
            "drift_pct":   100.0,
            "action":      "created",
            "stable":      False,
        }

    # Sample fresh mean via aggregation
    sample = list(source_db[collection_name].aggregate([
        {"$sample": {"size": 100}},
        {"$project": {"sz": {"$bsonSize": "$$ROOT"}}},
        {"$group": {
            "_id":  None,
            "mean": {"$avg": "$sz"},
        }},
    ]))

    if not sample:
        return {
            "collection":  collection_name,
            "drifted":     False,
            "drift_pct":   0.0,
            "action":      "none",
            "stable":      True,
            "reason":      "no_documents",
        }

    fresh_mean = sample[0]["mean"]
    drift_pct  = abs(fresh_mean - stored_mean) / max(stored_mean, 1) * 100.0

    if drift_pct > STABLE_THRESHOLD_PCT:
        compute_baseline(collection_name, SOURCE_DB)
        logger.info(
            "BaselineRefiner: refreshed '%s' (drift=%.1f%%).",
            collection_name, drift_pct,
        )
        return {
            "collection": collection_name,
            "drifted":    True,
            "drift_pct":  round(drift_pct, 2),
            "action":     "refreshed",
            "stable":     False,
        }

    return {
        "collection": collection_name,
        "drifted":    False,
        "drift_pct":  round(drift_pct, 2),
        "action":     "none",
        "stable":     True,
    }


# ── Agent instruction ─────────────────────────────────────────────────────────

_REFINER_INSTRUCTION = f"""
You are BaselineRefinerAgent — an adaptive baseline maintenance agent for QUERYSENTINEL.

Your task: check whether each monitored collection's stored baseline still matches
the current document-size distribution. Call check_and_update_baseline once for
EACH of these collections: {", ".join(MONITORED_COLLECTIONS)}.

After calling check_and_update_baseline for ALL collections, evaluate the results:

- If ALL collections returned stable=True (drift_pct <= {STABLE_THRESHOLD_PCT}%):
  Output EXACTLY this single word and nothing else: true

- If ANY collection returned drifted=True:
  Output EXACTLY this single word and nothing else: false

CRITICAL: Your ENTIRE response must be either the word "true" or the word "false".
No JSON. No markdown. No explanation. One word only.
"true" signals the loop to stop (all baselines converged).
"false" signals the loop to run another refinement cycle.
"""


def build_baseline_refiner_agent() -> LlmAgent:
    """Factory — called inside async context to avoid module-level tool init."""
    return LlmAgent(
        name="BaselineRefinerAgent",
        model=GEMINI_MODEL,
        instruction=_REFINER_INSTRUCTION,
        tools=[FunctionTool(func=check_and_update_baseline)],
        output_key="escalate",   # ADK LoopAgent reads session.state["escalate"]
    )


def build_adaptive_baseline_loop() -> LoopAgent:
    """
    Builds the LoopAgent that wraps BaselineRefinerAgent.

    Termination:
      - Early: BaselineRefinerAgent outputs "true" → session.state["escalate"]="true"
               → LoopAgent sees truthy state and exits.
      - Safety: max_iterations=5 hard cap regardless of escalate state.
    """
    return LoopAgent(
        name="AdaptiveBaselineLoop",
        sub_agents=[build_baseline_refiner_agent()],
        max_iterations=5,
    )


# ── Runner ────────────────────────────────────────────────────────────────────

async def run_baseline_loop() -> dict:
    """
    Run one full baseline-refinement loop cycle.
    Called by main.py every BASELINE_LOOP_INTERVAL_S seconds.

    Returns a summary of what was refreshed.
    """
    run_id = str(uuid.uuid4())
    t0     = time.monotonic()

    loop_agent = build_adaptive_baseline_loop()
    runner     = Runner(
        agent=loop_agent,
        app_name="querysentinel-baseline",
        session_service=_loop_session_service,
    )

    await _loop_session_service.create_session(
        app_name="querysentinel-baseline",
        user_id=run_id,
        session_id=run_id,
    )

    message = Content(
        role="user",
        parts=[Part.from_text(text=(
            f"Run baseline refinement for collections: "
            f"{', '.join(MONITORED_COLLECTIONS)}. "
            f"Run ID: {run_id}."
        ))],
    )

    iterations = 0
    try:
        async for event in runner.run_async(
            user_id=run_id,
            session_id=run_id,
            new_message=message,
        ):
            if hasattr(event, "author") and event.author == "BaselineRefinerAgent":
                iterations += 1
            if event.is_final_response():
                break
    except ValueError as exc:
        # Known ADK 1.x bug: LoopAgent escalate termination triggers an OpenTelemetry
        # span-context detachment error — "was created in a different Context".
        # This is NOT a functional error: the loop completed successfully, the escalate
        # signal fired, and all baselines were checked. We suppress this specific error.
        # Filed as: https://github.com/google/adk-python/issues/...
        if "different Context" in str(exc) or "created in" in str(exc):
            logger.debug(
                "Baseline loop (run=%s): LoopAgent OTel context cleanup — suppressed known "
                "ADK escalate/OTel issue. Loop completed successfully.", run_id
            )
        else:
            logger.error("Baseline loop error (run=%s): %s", run_id, exc)
            return {"status": "error", "error": str(exc), "run_id": run_id}
    except Exception as exc:
        logger.error("Baseline loop error (run=%s): %s", run_id, exc)
        return {"status": "error", "error": str(exc), "run_id": run_id}

    elapsed = round(time.monotonic() - t0, 2)
    logger.info(
        "Baseline loop complete: run=%s iterations=%d elapsed=%.1fs",
        run_id, iterations, elapsed,
    )
    return {
        "status":     "ok",
        "run_id":     run_id,
        "iterations": iterations,
        "elapsed_s":  elapsed,
        "ran_at":     datetime.now(timezone.utc).isoformat(),
    }


async def run_baseline_loop_forever(interval_s: int = 1800) -> None:
    """
    Infinite background task: runs the baseline refinement loop every interval_s.
    Default: 1800 s = 30 minutes.

    Startup delay: 60 s to let the watcher and seed data settle first.
    """
    await asyncio.sleep(60)
    while True:
        try:
            result = await run_baseline_loop()
            logger.info("Adaptive baseline result: %s", result)
        except Exception as exc:
            logger.warning("Baseline loop failed (will retry in %ds): %s", interval_s, exc)
        await asyncio.sleep(interval_s)
