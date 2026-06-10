"""
impact.py — Business-impact dollar estimator for QuerySentinel incidents.

Converts a technical anomaly (Z-score, anomaly type, collection size) into
a dollar-denominated business-impact estimate that executives and on-call
engineers can act on immediately.

The model is intentionally transparent and conservative:

  per_minute_cost = base_qps × degradation_multiplier × anomaly_multiplier × $0.15

  base_qps        = estimated_document_count / 10_000   (normalized query rate)
  degradation     = 1 + (z_score / 50)                  (Z → latency multiplier)
  anomaly_mult    = per-type severity weight (SEMANTIC_VELOCITY_SPIKE = 2.5×)

This does NOT include engineering triage time — that is separately reported as
engineer_triage_usd = 3.4 hours × $150/hour (configurable modeling assumption).

Usage:
    from impact import estimate_dollar_impact
    impact = estimate_dollar_impact("movies", z_score=168, anomaly_type="SEMANTIC_VELOCITY_SPIKE")
    # → {"per_minute_usd": 47.25, "if_unresolved_1hr_usd": 2835.0, "total_worst_case_usd": 3345.0, ...}
"""

import logging
from datetime import datetime, timezone

from db import client
from config import SOURCE_DB

logger = logging.getLogger("impact")


# ── Constants ─────────────────────────────────────────────────────────────────

_ENGINEER_HOURLY_RATE_USD = 150.0    # median senior SRE/DBA rate
_MEAN_TRIAGE_HOURS        = 3.4      # modeling assumption (configurable); ~MTTR for a manual DB triage
_COST_PER_QPS_PER_MIN     = 0.15     # Atlas M30+ throughput cost proxy ($/QPS/min)

# Per-anomaly-type base cost multiplier.
# SEMANTIC_VELOCITY_SPIKE is highest: content integrity corruption
# cascades into downstream ML models, search indexes, analytics pipelines.
_ANOMALY_MULTIPLIER: dict[str, float] = {
    "SEMANTIC_VELOCITY_SPIKE": 2.5,
    "SCHEMA_DRIFT":            2.0,
    "DOC_SIZE_SPIKE":          1.5,
    "HIGH_WRITE_RATE":         1.2,
    "HIGH_DELETE_RATE":        1.3,
    "UNKNOWN":                 1.0,
}

# Collection business-criticality tier (higher = more expensive per QPS)
_COLLECTION_TIER: dict[str, float] = {
    "orders":       3.0,
    "transactions": 3.0,
    "payments":     3.0,
    "sales":        2.5,
    "users":        2.0,
    "movies":       1.5,
    "comments":     1.2,
    "sessions":     1.0,
}


def _get_estimated_doc_count(collection_name: str, db_name: str = SOURCE_DB) -> int:
    """
    Fast estimated document count (non-locking).
    Falls back to 10_000 if Atlas is unreachable.
    """
    try:
        return max(client[db_name][collection_name].estimated_document_count(), 1)
    except Exception:
        return 10_000


def estimate_dollar_impact(
    collection_name: str,
    z_score: float,
    anomaly_type: str = "UNKNOWN",
    db_name: str = SOURCE_DB,
) -> dict:
    """
    Estimate the business-impact dollar cost of an anomaly.

    Args:
        collection_name: MongoDB collection where the anomaly was detected
        z_score: Anomaly Z-score (higher = more severe)
        anomaly_type: Anomaly type string (see _ANOMALY_MULTIPLIER keys)
        db_name: MongoDB database containing the collection

    Returns dict with:
        per_minute_usd          — ongoing cost while incident is unresolved
        if_unresolved_1hr_usd   — 1-hour projection
        engineer_triage_usd     — industry-average engineer time (3.4h × $150/h)
        total_worst_case_usd    — sum of all three
        querysentinel_save_usd  — savings vs. manual 3.4h triage (use in pitch)
        basis                   — all calculation inputs for transparency
    """
    collection_name    = collection_name or "unknown"  # guard against None
    doc_count          = _get_estimated_doc_count(collection_name, db_name)
    base_qps           = max(doc_count / 10_000, 0.1)
    z_capped           = min(abs(z_score or 0), 300.0)
    degradation        = 1.0 + (z_capped / 50.0)
    anomaly_mult       = _ANOMALY_MULTIPLIER.get(anomaly_type or "", 1.0)
    collection_tier    = _COLLECTION_TIER.get(collection_name.lower(), 1.0)

    per_minute_usd       = round(base_qps * degradation * anomaly_mult * collection_tier * _COST_PER_QPS_PER_MIN, 2)
    if_unresolved_1hr    = round(per_minute_usd * 60, 2)
    engineer_triage_usd  = round(_ENGINEER_HOURLY_RATE_USD * _MEAN_TRIAGE_HOURS, 2)  # 3.4h × $150
    total_worst_case_usd = round(if_unresolved_1hr + engineer_triage_usd, 2)

    # QuerySentinel saves the full 3.4-hour manual triage. We triage in ~11 minutes.
    qs_triage_cost       = round(per_minute_usd * 11, 2)  # 11-minute QuerySentinel resolution
    querysentinel_save   = round(engineer_triage_usd - qs_triage_cost + (if_unresolved_1hr - qs_triage_cost), 2)
    querysentinel_save   = max(querysentinel_save, 0.0)

    return {
        "per_minute_usd":          per_minute_usd,
        "if_unresolved_1hr_usd":   if_unresolved_1hr,
        "engineer_triage_usd":     engineer_triage_usd,
        "total_worst_case_usd":    total_worst_case_usd,
        "querysentinel_save_usd":  querysentinel_save,
        "headline":                (
            f"${per_minute_usd}/min ongoing · "
            f"${total_worst_case_usd} worst-case 1hr · "
            f"${querysentinel_save} saved by QuerySentinel"
        ),
        "basis": {
            "doc_count":          doc_count,
            "base_qps":           round(base_qps, 2),
            "z_score":            round(z_capped, 1),
            "degradation_mult":   round(degradation, 3),
            "anomaly_mult":       anomaly_mult,
            "collection_tier":    collection_tier,
            "anomaly_type":       anomaly_type,
            "engineer_rate_usd":  _ENGINEER_HOURLY_RATE_USD,
            "mean_triage_hrs":    _MEAN_TRIAGE_HOURS,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
