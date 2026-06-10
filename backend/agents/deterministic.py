"""
deterministic.py — LLM-free incident builder (graceful degradation).

QuerySentinel's DETECTION is deterministic and always-on: regex AI-poisoning
patterns, semantic-velocity centroid drift, $bsonSize z-scores, schema diff, and
Atlas $vectorSearch — none of these need an LLM. The 5-6 Gemini agents add a
human-readable REASONING narrative on top.

This module produces a complete, useful incident report from the detection
signals alone. The pipeline always computes this baseline first; if the LLM is
available it enriches/overrides the fields, and if the LLM is throttled (429) or
absent the incident is STILL complete. Defense never depends on a model being up.

Returns the same field shape the agents emit:
  context, schema_drift, similar_incidents, root_cause, remediation
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("deterministic")


# ── Threat-aware root-cause + remediation templates (rule-based) ──────────────

_ROOT_CAUSE = {
    "AI_MEMORY_POISONING": (
        "Prompt-injection content was written into the collection — poisoning the "
        "memory layer that downstream AI agents read (OWASP LLM01)."),
    "SEMANTIC_VELOCITY_SPIKE": (
        "The collection's semantic centroid drifted sharply this hour: documents no "
        "longer match the historical meaning, consistent with data-source contamination "
        "or training-data poisoning (OWASP LLM03)."),
    "SCHEMA_DRIFT": (
        "The live schema diverged from the stored baseline (new/changed fields), "
        "consistent with an upstream writer change or supply-chain drift (OWASP LLM05)."),
    "DOC_SIZE_SPIKE": (
        "A document's size is far above the collection baseline, consistent with "
        "injected payloads or write amplification (OWASP LLM04)."),
    "ETL_MISCONFIGURATION": (
        "Ingestion pipeline wrote semantically alien content into the collection."),
}

_REMEDIATION = {
    "AI_MEMORY_POISONING": [
        {"rank": 1, "title": "Quarantine the poisoned document",
         "description": "Move the flagged document to a quarantine collection and remove it "
                        "from the agent-readable path. Alert the security team.",
         "estimated_minutes": 5, "mcp_action": "delete-many", "requires_approval": True},
        {"rank": 2, "title": "Audit recent writes from the same source",
         "description": "Scan the last hour of writes for the same injection signature.",
         "estimated_minutes": 15, "mcp_action": "find", "requires_approval": False},
        {"rank": 3, "title": "Add a write-time injection filter",
         "description": "Reject documents matching OWASP LLM01 patterns at ingestion.",
         "estimated_minutes": 30, "mcp_action": "application-fix", "requires_approval": False},
    ],
    "SEMANTIC_VELOCITY_SPIKE": [
        {"rank": 1, "title": "Isolate the drifted documents",
         "description": "Freeze the affected collection from agent retrieval until reviewed.",
         "estimated_minutes": 10, "mcp_action": "application-fix", "requires_approval": True},
        {"rank": 2, "title": "Review the ingestion source",
         "description": "Inspect what wrote the divergent content this hour.",
         "estimated_minutes": 20, "mcp_action": "find", "requires_approval": False},
        {"rank": 3, "title": "Re-embed with a pinned model version",
         "description": "Pin the embedding model to rule out model-mismatch drift.",
         "estimated_minutes": 30, "mcp_action": "application-fix", "requires_approval": False},
    ],
    "SCHEMA_DRIFT": [
        {"rank": 1, "title": "Validate the new schema",
         "description": "Confirm the added/changed fields are intentional.",
         "estimated_minutes": 10, "mcp_action": "collection-schema", "requires_approval": False},
        {"rank": 2, "title": "Add a schema-validation rule",
         "description": "Enforce the expected shape at write time.",
         "estimated_minutes": 20, "mcp_action": "application-fix", "requires_approval": True},
    ],
    "DOC_SIZE_SPIKE": [
        {"rank": 1, "title": "Investigate the oversized document",
         "description": "Inspect the flagged document for injected or corrupt payloads.",
         "estimated_minutes": 10, "mcp_action": "find", "requires_approval": False},
        {"rank": 2, "title": "Add a document-size guard",
         "description": "Reject writes above a size threshold at ingestion.",
         "estimated_minutes": 20, "mcp_action": "application-fix", "requires_approval": True},
    ],
}

_DEFAULT_REMEDIATION = [
    {"rank": 1, "title": "Investigate the flagged document",
     "description": "Manually review the anomaly and confirm root cause.",
     "estimated_minutes": 15, "mcp_action": "find", "requires_approval": False},
]


def deterministic_content(anomaly: dict, app_db=None) -> dict:
    """
    Build a complete incident body from detection signals — no LLM required.
    Used as the always-on baseline; LLM agents enrich/override when available.

    GUARANTEE: this function never raises. On any internal error it still returns
    the full 5-field shape (with safe defaults), so an incident is always complete.
    """
    try:
        return _deterministic_content_inner(anomaly, app_db)
    except Exception as e:  # absolute safety net — incident must never be empty
        logger.warning("deterministic_content fell back to minimal (%s)", e)
        atype = anomaly.get("anomaly_type", "UNKNOWN")
        coll = anomaly.get("collection_name", "")
        return {
            "context":           {"anomaly_summary": f"{atype} in '{coll}'", "deterministic": True},
            "schema_drift":      {"drift_detected": False, "deterministic": True},
            "similar_incidents": {"matches": [], "deterministic": True},
            "root_cause":        {"root_cause_description": _ROOT_CAUSE.get(atype, f"{atype} detected."),
                                  "confidence": 0.5, "deterministic": True},
            "remediation":       {"options": _DEFAULT_REMEDIATION, "recommended_option": 0,
                                  "immediate_action": _DEFAULT_REMEDIATION[0]["title"],
                                  "deterministic": True},
        }


def _deterministic_content_inner(anomaly: dict, app_db=None) -> dict:
    atype = anomaly.get("anomaly_type", "UNKNOWN")
    coll = anomaly.get("collection_name", "")
    sev = anomaly.get("severity", "WARNING")
    z = float(anomaly.get("z_score", 0) or 0)

    # ── context (deterministic) ───────────────────────────────────────────────
    context = {
        "flagged_doc_id":  anomaly.get("document_id", ""),
        "z_score":         round(z, 2),
        "anomaly_summary": (
            f"{sev} {atype.replace('_', ' ').title()} in '{coll}' "
            f"(z={z:.1f})." + (
                " Document contains prompt-injection content."
                if atype == "AI_MEMORY_POISONING" else
                " Semantic meaning diverged from historical norm."
                if atype == "SEMANTIC_VELOCITY_SPIKE" else "")
        ),
        "ai_threat_matches": anomaly.get("ai_threat_matches", []),
        "deterministic": True,
    }

    # ── schema_drift (deterministic — detect.py, no LLM) ──────────────────────
    schema_drift = {}
    try:
        from detect import detect_schema_drift
        schema_drift = detect_schema_drift(coll) or {}
        schema_drift["deterministic"] = True
    except Exception as e:
        logger.debug("schema drift (det) skipped: %s", e)
        schema_drift = {"drift_detected": False, "deterministic": True}

    # ── similar_incidents (deterministic — real $vectorSearch, no LLM) ────────
    similar = {"matches": [], "deterministic": True}
    try:
        from .direct_pipeline import _pre_fetch_similar
        if app_db is None:
            from db import app_db as _adb
            app_db = _adb
        matches = _pre_fetch_similar(anomaly, app_db)
        similar = {
            "matches": matches,
            "top_match_score": matches[0].get("score") if matches else 0,
            "pattern_identified": matches[0].get("anomaly_type") if matches else atype,
            "estimated_resolution_minutes": (
                matches[0].get("resolution_time_minutes") if matches else 30),
            "deterministic": True,
        }
    except Exception as e:
        logger.debug("similar (det) skipped: %s", e)

    # ── root_cause (templated, threat-aware) ──────────────────────────────────
    root_cause = {
        "root_cause_description": _ROOT_CAUSE.get(
            atype, f"{atype} detected on '{coll}'; manual review recommended."),
        "confidence": 0.75 if atype in _ROOT_CAUSE else 0.5,
        "category": (
            "security_threat" if atype in ("AI_MEMORY_POISONING", "SEMANTIC_VELOCITY_SPIKE")
            else "data_pipeline" if atype in ("SCHEMA_DRIFT", "ETL_MISCONFIGURATION")
            else "infrastructure"),
        "deterministic": True,
    }

    # ── remediation (templated, threat-aware) ─────────────────────────────────
    options = _REMEDIATION.get(atype, _DEFAULT_REMEDIATION)
    remediation = {
        "approve_required": True,
        "options": options,
        "recommended_option": 0,
        "immediate_action": options[0]["title"],
        "escalate": sev == "CRITICAL",
        "deterministic": True,
    }

    return {
        "context":           context,
        "schema_drift":      schema_drift,
        "similar_incidents": similar,
        "root_cause":        root_cause,
        "remediation":       remediation,
    }


def merge_llm_over_deterministic(deterministic: dict, llm: dict) -> tuple[dict, bool]:
    """
    Fill any empty/failed LLM field with the deterministic baseline.
    Returns (merged, llm_enriched) — llm_enriched=True if the LLM contributed
    at least one real field.
    """
    merged = {}
    enriched = False
    for key, det_val in deterministic.items():
        llm_val = (llm or {}).get(key)
        good = (
            isinstance(llm_val, dict)
            and llm_val
            and not llm_val.get("parse_error")
            and not llm_val.get("raw_response")
        )
        if good:
            merged[key] = llm_val
            enriched = True
        else:
            merged[key] = det_val
    return merged, enriched
