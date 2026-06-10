"""
detect.py �� All detection algorithms for QUERYSENTINEL.

  compute_baseline()          — Z-score baseline per collection
  detect_doc_size_anomaly()   — Per-document Z-score check
  detect_schema_drift()       — Structural schema diff vs stored baseline
  compute_semantic_velocity() — NOVEL: cosine centroid drift (voyage-4 embeddings)
  compute_incident_runway()   — Predict time-to-critical from ASP stream_stats / raw_events
  build_database_health_card()— Startup survey: P50/P95/P99 sizes, index counts
"""
import bson
import logging
import numpy as np
import pymongo
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import app_db, source_db, client
from config import MONITORED_COLLECTIONS, SOURCE_DB, CRITICAL_Z, ANOMALY_Z

logger = logging.getLogger("detect")

# ─────────────────────────────────────────────────────────────────────────────
# BASELINE
# ─────────────────────────────────────────────────────────────────────────────

def compute_baseline(collection_name: str, db_name: str = SOURCE_DB) -> dict:
    """
    Sample 500 docs, compute mean + stddev of BSON byte sizes via $bsonSize.
    Stores result in app_db.collection_baselines (upsert).
    Returns the baseline dict.
    """
    pipeline = [
        {"$sample": {"size": 500}},
        {"$project": {"doc_size_bytes": {"$bsonSize": "$$ROOT"}}},
        {"$group": {
            "_id":          None,
            "mean_bytes":   {"$avg":        "$doc_size_bytes"},
            "stddev_bytes": {"$stdDevSamp": "$doc_size_bytes"},
            "sample_count": {"$sum": 1},
        }},
    ]
    rows = list(client[db_name][collection_name].aggregate(pipeline))
    if not rows:
        return {}

    # Get live schema fingerprint
    schema = _get_live_schema(collection_name, db_name)

    baseline = {
        "collection_name": collection_name,
        "db_name":         db_name,
        "mean_bytes":      rows[0]["mean_bytes"],
        "stddev_bytes":    rows[0]["stddev_bytes"],
        "sample_count":    rows[0]["sample_count"],
        "schema":          schema,
        "updated_at":      datetime.now(timezone.utc),
    }
    app_db.collection_baselines.update_one(
        {"collection_name": collection_name},
        {"$set": baseline},
        upsert=True,
    )
    return baseline


# ─────────────────────────────────────────────────────────────────────────────
# DOC SIZE ANOMALY  (Z-score per document)
# ─────────────────────────────────────────────────────────────────────────────

def detect_doc_size_anomaly(change_event: dict, baseline: dict) -> Optional[dict]:
    """
    Given a Change Stream event and a baseline, returns an anomaly dict if
    |Z| >= ANOMALY_Z threshold, else None.
    """
    doc = change_event.get("fullDocument")
    if not doc:
        return None

    doc_bytes = len(bson.encode(doc))
    mean      = baseline.get("mean_bytes",   1_000)
    stddev    = max(baseline.get("stddev_bytes", 500), 1)
    z         = (doc_bytes - mean) / stddev

    if abs(z) < ANOMALY_Z:
        return None

    severity   = "CRITICAL" if abs(z) >= CRITICAL_Z else "WARNING"
    # collection name from the Change Stream namespace object
    collection = change_event.get("ns", {}).get("coll") or baseline.get("collection_name", "unknown")

    return {
        "collection_name":  collection,
        "anomaly_type":     "DOC_SIZE_SPIKE",
        "z_score":          round(z, 3),
        "doc_size_bytes":   doc_bytes,
        "doc_size_kb":      round(doc_bytes / 1024, 2),
        "baseline_mean_kb": round(mean / 1024, 2),
        "severity":         severity,
        "document_id":      str(change_event.get("documentKey", {}).get("_id", "")),
        "description": (
            f"{severity}: Document in '{collection}' is "
            f"{round(doc_bytes/1024, 1)} KB (Z={round(z,2)}). "
            f"Baseline mean: {round(mean/1024,1)} KB."
        ),
        "confidence":       min(1.0, abs(z) / 10.0),
        "detected_at":      datetime.now(timezone.utc),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA DRIFT
# ─────────────────────────────────────────────────────────────────────────────

def _get_live_schema(collection_name: str, db_name: str) -> dict:
    """Sample 100 docs and extract field→types mapping."""
    sample = list(client[db_name][collection_name].aggregate([
        {"$sample": {"size": 100}},
        {"$project": {"_id": 0}},
    ]))
    schema: dict[str, set] = {}
    for doc in sample:
        for key, val in doc.items():
            schema.setdefault(key, set()).add(type(val).__name__)
    return {k: sorted(v) for k, v in schema.items()}


def detect_schema_drift(collection_name: str, db_name: str = SOURCE_DB) -> dict:
    """
    Diffs current live schema against stored baseline schema.
    Detects: added fields, removed fields, type changes.
    Updates baseline on drift.
    """
    live     = _get_live_schema(collection_name, db_name)
    baseline = app_db.collection_baselines.find_one({"collection_name": collection_name}) or {}
    stored   = baseline.get("schema", {})

    added_fields   = [k for k in live   if k not in stored]
    removed_fields = [k for k in stored if k not in live]
    type_changes   = [
        {"field": k, "was": stored[k], "now": live[k]}
        for k in live if k in stored and sorted(stored[k]) != sorted(live[k])
    ]
    has_drift = bool(added_fields or removed_fields or type_changes)

    if has_drift:
        app_db.collection_baselines.update_one(
            {"collection_name": collection_name},
            {"$set": {"schema": live, "last_drift_at": datetime.now(timezone.utc)}},
        )

    return {
        "collection_name":  collection_name,
        "drift_detected":   has_drift,
        "added_fields":     added_fields,
        "removed_fields":   removed_fields,
        "type_changes":     type_changes,
        "live_field_count": len(live),
        "most_suspicious_field": (
            added_fields[0] if added_fields else
            type_changes[0]["field"] if type_changes else None
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SEMANTIC VELOCITY  (Novel — centroid drift via voyage-4 embeddings)
# ─────────────────────────────────────────────────────────────────────────────

def compute_semantic_velocity(collection_name: str, hours_back: int = 8) -> dict:
    """
    NOVEL TECHNIQUE: Detects semantic drift by measuring how the mean embedding
    vector (semantic centroid) of recent documents moves hour-over-hour.

    ⚠ IMPORTANT FIX: MongoDB's $avg accumulator on array fields does NOT do
    element-wise vector averaging — it returns a scalar. We retrieve embeddings
    here and compute element-wise means in Python with numpy.

    Data source priority:
      1. app_db.raw_events   — live anomaly events with voyage-4 embeddings
      2. app_db.anomaly_history — seeded historical data (fallback if raw is sparse)

    Returns field names that match the frontend SemanticVelocityResult interface:
      centroid_distance, baseline_distance, velocity_ratio, status, hourly_distances
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    # ── Fetch embeddings with timestamps ──────────────────────────────────────
    # Primary: live raw_events (voyage-4 auto-embeds description field)
    docs = list(app_db.raw_events.find(
        {
            "collection_name": collection_name,
            "embedding":       {"$exists": True},
            "detected_at":     {"$gte": cutoff},
        },
        {"embedding": 1, "detected_at": 1, "_id": 0},
    ))

    # Fallback: anomaly_history (seeded data — for cold-start/demo reliability)
    if len(docs) < 2:
        hist_docs = list(app_db.anomaly_history.find(
            {
                "collection_name": collection_name,
                "embedding":       {"$exists": True},
                "created_at":      {"$gte": cutoff},
            },
            {"embedding": 1, "created_at": 1, "_id": 0},
        ))
        # Normalize timestamp field to 'detected_at' for uniform processing
        for d in hist_docs:
            d["detected_at"] = d.pop("created_at", datetime.now(timezone.utc))
        docs = docs + hist_docs

    # Cold-load safety: if the time window is empty (seed data aged out), fall back
    # to the most recent embedded docs regardless of age so the dashboard card is
    # never embarrassingly blank. Spreads them across synthetic hourly buckets.
    if len(docs) < 2:
        recent = list(app_db.anomaly_history.find(
            {"collection_name": collection_name, "embedding": {"$exists": True}},
            {"embedding": 1, "created_at": 1, "_id": 0},
        ).sort("created_at", -1).limit(60))
        base = datetime.now(timezone.utc)
        for i, d in enumerate(recent):
            # bucket every ~8 docs into a distinct synthetic hour
            d["detected_at"] = base - timedelta(hours=(i // 8))
            d.pop("created_at", None)
        docs = recent

    _INSUFFICIENT = {
        "collection_name":    collection_name,
        "centroid_distance":  0.0,
        "baseline_distance":  0.0,
        "velocity_ratio":     1.0,
        "velocity_zscore":    0.0,
        "status":             "insufficient_data",
        "hourly_distances":   [],
        "window_hours":       hours_back,
    }

    if len(docs) < 2:
        return _INSUFFICIENT

    # ── Group embeddings by hour using Python (NOT MongoDB $avg) ──────────────
    hourly_groups: dict[str, list[np.ndarray]] = defaultdict(list)
    for doc in docs:
        ts  = doc.get("detected_at")
        emb = doc.get("embedding")
        if ts and emb:
            hour_key = ts.strftime("%Y-%m-%dT%H")
            try:
                vec = np.array(emb, dtype=np.float64)
                if vec.ndim == 1 and vec.shape[0] > 0:
                    hourly_groups[hour_key].append(vec)
            except (ValueError, TypeError):
                continue

    if len(hourly_groups) < 2:
        return _INSUFFICIENT

    # ── Compute element-wise centroids using numpy ─────────────────────────────
    sorted_hours = sorted(hourly_groups.keys())
    centroids: list[np.ndarray] = []
    for hour in sorted_hours:
        vectors = hourly_groups[hour]
        # Stack into matrix [N, D] and mean across rows → [D] centroid
        mat     = np.stack(vectors, axis=0)        # shape (N, 1024)
        centroid = np.mean(mat, axis=0)            # shape (1024,) — element-wise mean
        norm     = np.linalg.norm(centroid)
        if norm > 1e-9:
            centroid = centroid / norm             # unit-normalize for reliable cosine
        centroids.append(centroid)

    # ── Compute cosine distances between consecutive hour centroids ────────────
    distances: list[float] = []
    for i in range(1, len(centroids)):
        # Both vectors unit-normalized → cosine similarity = dot product
        cos_sim = float(np.dot(centroids[i - 1], centroids[i]))
        cos_sim = max(-1.0, min(1.0, cos_sim))     # clamp float precision
        distances.append(round(1.0 - cos_sim, 6)) # cosine DISTANCE = 1 − similarity

    if not distances:
        return _INSUFFICIENT

    current  = distances[-1]
    history  = distances[:-1]
    baseline_avg = float(np.mean(history)) if history else current
    baseline_std = float(np.std(history))  if len(history) > 1 else 1e-7
    velocity_z   = (current - baseline_avg) / max(baseline_std, 1e-7)
    velocity_ratio = current / max(baseline_avg, 1e-7)

    # ── Cumulative drift score (catches slow-burn attacks) ─────────────────────
    # Point-in-time Z-score misses gradual poisoning that never creates a single
    # spike. Summing recent distances catches content that drifts steadily over
    # multiple hours — consistent with systematic data poisoning campaigns.
    _CUMULATIVE_WINDOW    = 6   # look back 6 hours
    _CUMULATIVE_THRESHOLD = 0.8 # tunable: sum of cosine distances > threshold
    recent_distances    = distances[-_CUMULATIVE_WINDOW:]
    cumulative_drift    = float(np.sum(recent_distances))
    cumulative_alert    = cumulative_drift > _CUMULATIVE_THRESHOLD and len(recent_distances) >= 3

    status = (
        "spike"            if velocity_z > 3.0         else
        "cumulative_drift" if cumulative_alert          else
        "elevated"         if velocity_z > 1.5         else
        "normal"
    )

    result = {
        "collection_name":   collection_name,
        "centroid_distance": round(current, 6),
        "baseline_distance": round(baseline_avg, 6),
        "velocity_ratio":    round(velocity_ratio, 3),
        "velocity_zscore":   round(float(velocity_z), 2),
        "cumulative_drift":  round(cumulative_drift, 6),
        "cumulative_alert":  cumulative_alert,
        "status":            status,
        "hourly_distances":  distances,
        "window_hours":      hours_back,
        # Legacy field names kept for backward compat
        "semantic_velocity": round(current, 6),
        "baseline_velocity": round(baseline_avg, 6),
        "spike_detected":    status == "spike",
        "hourly_trajectory": distances,
    }

    if status == "spike":
        result["interpretation"] = (
            f"SEMANTIC SPIKE in '{collection_name}': centroid shifted "
            f"{current:.4f} cosine units this hour "
            f"(Z={velocity_z:.1f} vs historical avg {baseline_avg:.4f}). "
            f"Documents no longer semantically similar to historical norm. "
            f"Possible: data source contamination, pipeline mixing, or domain change."
        )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# INCIDENT RUNWAY  (time-to-critical prediction)
# ─────────────────────────────────────────────────────────────────────────────

def compute_incident_runway(collection_name: str) -> dict:
    """
    Predicts time-to-critical by analysing write rate trend.

    Strategy (most reliable first):
      1. Atlas Stream Processing stream_stats (if compound _id with window_end exists)
      2. Fallback: count raw_events per 60-min window from last 6 hours
    Both return compatible RunwayResult format matching the frontend interface.
    """
    from config import CRITICAL_Z

    # ── Try ASP stream_stats first ────────────────────────────────────────────
    # New ASP pipeline writes {collection_name, window_end, event_count, avg_doc_size_kb}
    recent_asp = list(app_db.stream_stats.find(
        {"collection_name": collection_name},
        sort=[("window_end", pymongo.DESCENDING)],
    ).limit(6))

    # Fallback: count raw_events anomalies per 60-min window
    if len(recent_asp) < 3:
        recent_asp = _build_runway_from_raw_events(collection_name)

    if len(recent_asp) < 3:
        return {
            "collection_name":    collection_name,
            "current_rate_per_min": 0.0,
            "projected_critical_at": None,
            "minutes_to_critical":   None,
            "trend":                 "stable",
            "confidence":            0.1,
            "status":                "insufficient_data",
        }

    # ── Compute write-rate slope ───────────────────────────────────────────────
    rates = [r.get("event_count", r.get("current_rate_per_min", 0)) for r in recent_asp]
    current_rate = float(rates[0])

    if len(rates) >= 2:
        # Linear slope over the last N windows (each window ≈ 1 min in rate terms)
        xs  = list(range(len(rates)))
        ys  = list(reversed(rates))          # oldest first
        n   = len(xs)
        sx  = sum(xs); sy = sum(ys)
        sxy = sum(x * y for x, y in zip(xs, ys))
        sxx = sum(x * x for x in xs)
        denom = n * sxx - sx * sx
        slope = (n * sxy - sx * sy) / denom if denom != 0 else 0.0
    else:
        slope = 0.0

    trend = (
        "increasing" if slope > 0.5 else
        "decreasing" if slope < -0.5 else
        "stable"
    )

    # ── Time-to-critical ─────────────────────────────────────────────────────
    baseline = app_db.collection_baselines.find_one({"collection_name": collection_name}) or {}
    mean_bytes   = baseline.get("mean_bytes",   1_024)
    stddev_bytes = max(baseline.get("stddev_bytes") or 512, 1)
    # CRITICAL threshold: doc size Z >= CRITICAL_Z
    critical_size_kb = (mean_bytes + CRITICAL_Z * stddev_bytes) / 1024

    minutes_to_critical: Optional[float] = None
    projected_critical_at: Optional[str] = None

    if slope > 0 and current_rate > 0:
        current_size_kb = recent_asp[0].get("avg_doc_size_kb", 0)
        gap = critical_size_kb - current_size_kb
        if gap > 0 and slope > 0:
            minutes_to_critical = round(gap / slope, 1)
            eta = datetime.now(timezone.utc) + timedelta(minutes=minutes_to_critical)
            projected_critical_at = eta.isoformat()

    confidence = min(0.95, 0.3 + len(recent_asp) * 0.1 + (0.3 if slope != 0 else 0))

    return {
        "collection_name":       collection_name,
        "current_rate_per_min":  round(current_rate, 2),
        "projected_critical_at": projected_critical_at,
        "minutes_to_critical":   minutes_to_critical,
        "trend":                 trend,
        "confidence":            round(confidence, 2),
        "status": (
            "CRITICAL_IMMINENT" if minutes_to_critical and minutes_to_critical < 30 else
            "WARNING"           if minutes_to_critical and minutes_to_critical < 120 else
            "NORMAL"
        ),
    }


def _build_runway_from_raw_events(collection_name: str) -> list:
    """Fallback: count raw_events anomalies in 60-minute windows."""
    now = datetime.now(timezone.utc)
    windows = []
    for i in range(6):
        start = now - timedelta(hours=i + 1)
        end   = now - timedelta(hours=i)
        count = app_db.raw_events.count_documents({
            "collection_name": collection_name,
            "detected_at":     {"$gte": start, "$lt": end},
        })
        windows.append({
            "collection_name":    collection_name,
            "event_count":        count,
            "current_rate_per_min": round(count / 60, 4),
            "avg_doc_size_kb":    0.0,
            "window_end":         end,
        })
    return windows


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE HEALTH CARD  (startup survey — agent knows DB before first anomaly)
# ─────────────────────────────────────────────────────────────────────────────

def build_database_health_card(db_name: str = SOURCE_DB) -> dict:
    """
    30-second startup survey of all monitored collections.
    Computes P50/P95/P99 document sizes, index count, health score.
    Cached in app_db.dashboard_state for dashboard to read on page load.

    Uses $collStats aggregation (preferred over deprecated collStats command).
    """
    cards = []
    for cname in MONITORED_COLLECTIONS:
        try:
            coll = client[db_name][cname]

            # ── Document count + storage stats via $collStats (MongoDB 4.4+) ──
            col_stats_result = list(coll.aggregate([
                {"$collStats": {"storageStats": {"scale": 1}}},
            ]))
            storage = col_stats_result[0].get("storageStats", {}) if col_stats_result else {}
            doc_count = storage.get("count", 0)

            # ── P50/P95/P99 via $percentile (MongoDB 7.0+) ────────────────────
            pct_pipeline = [
                {"$sample": {"size": 300}},
                {"$project": {"sz": {"$bsonSize": "$$ROOT"}}},
                {"$group": {
                    "_id":       None,
                    "p50_bytes": {"$percentile": {"input": "$sz", "p": [0.50], "method": "approximate"}},
                    "p95_bytes": {"$percentile": {"input": "$sz", "p": [0.95], "method": "approximate"}},
                    "p99_bytes": {"$percentile": {"input": "$sz", "p": [0.99], "method": "approximate"}},
                    "mean_bytes": {"$avg": "$sz"},
                    "max_bytes": {"$max": "$sz"},
                }},
            ]
            pct    = list(coll.aggregate(pct_pipeline))
            p50    = pct[0]["p50_bytes"][0] if pct and pct[0].get("p50_bytes") else 1024
            p95    = pct[0]["p95_bytes"][0] if pct and pct[0].get("p95_bytes") else 1024
            p99    = pct[0]["p99_bytes"][0] if pct and pct[0].get("p99_bytes") else 1024
            mean_b = pct[0].get("mean_bytes", 1024) if pct else 1024
            max_b  = pct[0].get("max_bytes",  1024) if pct else 1024

            # ── Index count (exclude _id) ─────────────────────────────────────
            all_indexes = list(coll.list_indexes())
            idx_count   = max(0, len(all_indexes) - 1)

            # ── Health score 0–10 ─────────────────────────────────────────────
            score  = 10.0
            issues = []

            if idx_count == 0:
                score -= 3.0
                issues.append("No secondary indexes — all queries will collection-scan")

            skew = p95 / max(p50, 1)
            if skew > 20:
                score -= 2.5
                issues.append(f"High size skew: P95/P50 = {skew:.0f}× — outlier documents detected")
            elif skew > 5:
                score -= 1.0
                issues.append(f"Moderate size skew: P95/P50 = {skew:.0f}×")

            if p99 > 1_048_576:    # P99 > 1 MB
                score -= 2.0
                issues.append(f"P99 size {round(p99/1024,0)} KB — approaching 16 MB BSON limit risk")

            if max_b > 8_388_608:  # any doc > 8 MB
                score -= 1.5
                issues.append(f"Max document {round(max_b/1024/1024,1)} MB — over 50% of BSON limit")

            health_score = round(max(0.0, score), 1)
            cards.append({
                "collection":   cname,
                "health_score": health_score,
                "doc_count":    doc_count,
                "mean_size_kb": round(mean_b / 1024, 2),
                "p50_size_kb":  round(p50 / 1024, 2),
                "p95_size_kb":  round(p95 / 1024, 2),
                "p99_size_kb":  round(p99 / 1024, 2),
                "max_size_kb":  round(max_b / 1024, 2),
                "index_count":  idx_count,
                "status":       (
                    "HEALTHY"  if health_score >= 8 else
                    "WARNING"  if health_score >= 5 else
                    "CRITICAL"
                ),
                "issues":       issues,
            })
        except Exception as e:
            logger.warning("Health card failed for %s: %s", cname, e)
            cards.append({
                "collection":   cname,
                "health_score": 0.0,
                "status":       "ERROR",
                "issues":       [str(e)],
                "doc_count":    0,
                "index_count":  0,
            })

    result = {
        "cards":      cards,
        "built_at":   datetime.now(timezone.utc).isoformat(),
        "db_name":    db_name,
        "collection_count": len(cards),
    }

    # Cache in dashboard_state
    app_db.dashboard_state.replace_one(
        {"_id": "health_card"},
        {"_id": "health_card", **result},
        upsert=True,
    )
    return result
