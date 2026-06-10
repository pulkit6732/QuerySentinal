"""
baseline.py — Compute and persist baselines for all monitored collections.

Run once after connecting to Atlas (before first anomaly detection):
    python baseline.py

What it does:
  1. Computes size/field baseline for each monitored collection
  2. Stores results in app_db.collection_baselines
  3. Reports summary stats to console

Also generates collection_schema snapshots for SchemaDriftAgent comparison.
"""
import logging
import sys
from datetime import datetime, timezone

from pymongo import MongoClient, DESCENDING

from config import MONGODB_URI, MONITORED_COLLECTIONS, SOURCE_DB

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("baseline")


def _compute_single_baseline(source_db, app_db, collection_name: str) -> dict:
    """Compute and store baseline for one collection."""
    coll = source_db[collection_name]

    # ── Size stats via $bsonSize ──────────────────────────────────────────────
    size_pipeline = [
        {"$sample": {"size": 500}},
        {
            "$group": {
                "_id": None,
                "mean_bytes":   {"$avg":         {"$bsonSize": "$$ROOT"}},
                "stddev_bytes": {"$stdDevSamp":   {"$bsonSize": "$$ROOT"}},
                "p50_bytes":    {"$percentile":   {"input": {"$bsonSize": "$$ROOT"}, "p": [0.5],  "method": "approximate"}},
                "p95_bytes":    {"$percentile":   {"input": {"$bsonSize": "$$ROOT"}, "p": [0.95], "method": "approximate"}},
                "p99_bytes":    {"$percentile":   {"input": {"$bsonSize": "$$ROOT"}, "p": [0.99], "method": "approximate"}},
                "max_bytes":    {"$max":          {"$bsonSize": "$$ROOT"}},
                "min_bytes":    {"$min":          {"$bsonSize": "$$ROOT"}},
                "sample_count": {"$sum":          1},
            }
        },
    ]

    size_result = list(coll.aggregate(size_pipeline))
    if not size_result:
        logger.warning("No documents found in %s — skipping baseline.", collection_name)
        return {}

    s = size_result[0]

    # ── Field profile via $objectToArray ─────────────────────────────────────
    field_pipeline = [
        {"$sample": {"size": 200}},
        {"$project": {"fields": {"$objectToArray": "$$ROOT"}}},
        {"$unwind": "$fields"},
        {"$group": {"_id": "$fields.k", "count": {"$sum": 1}}},
        {"$sort":  {"count": DESCENDING}},
        {"$limit": 30},
    ]

    field_result = list(coll.aggregate(field_pipeline))
    top_fields = [{"field": f["_id"], "occurrence_count": f["count"]} for f in field_result]

    # ── Estimated document count ──────────────────────────────────────────────
    doc_count = coll.estimated_document_count()

    # ── Index info ────────────────────────────────────────────────────────────
    indexes = list(coll.list_indexes())
    index_names = [idx["name"] for idx in indexes]

    baseline = {
        "_id":             collection_name,
        "collection_name": collection_name,
        "db_name":         SOURCE_DB,
        "computed_at":     datetime.now(timezone.utc),
        "sample_count":    s["sample_count"],
        "doc_count_est":   doc_count,
        "mean_bytes":      s["mean_bytes"],
        "stddev_bytes":    s["stddev_bytes"],
        "p50_bytes":       s["p50_bytes"][0] if s.get("p50_bytes") else None,
        "p95_bytes":       s["p95_bytes"][0] if s.get("p95_bytes") else None,
        "p99_bytes":       s["p99_bytes"][0] if s.get("p99_bytes") else None,
        "max_bytes":       s["max_bytes"],
        "min_bytes":       s["min_bytes"],
        "top_fields":      top_fields,
        "index_names":     index_names,
        "index_count":     len(index_names),
        # Anomaly thresholds (precomputed for fast watcher comparison)
        "warning_threshold_bytes":  s["mean_bytes"] + 2.0 * s["stddev_bytes"],
        "critical_threshold_bytes": s["mean_bytes"] + 3.5 * s["stddev_bytes"],
    }

    # Upsert into collection_baselines
    app_db.collection_baselines.replace_one(
        {"_id": collection_name},
        baseline,
        upsert=True,
    )

    return baseline


def _snapshot_schema(source_db, app_db, collection_name: str) -> None:
    """Store a schema snapshot for later drift comparison by SchemaDriftAgent."""
    coll = source_db[collection_name]

    # Sample field types from 100 documents
    pipeline = [
        {"$sample": {"size": 100}},
        {"$project": {"fields": {"$objectToArray": "$$ROOT"}}},
        {"$unwind": "$fields"},
        {
            "$group": {
                "_id": "$fields.k",
                "types": {"$addToSet": {"$type": "$fields.v"}},
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"count": DESCENDING}},
    ]

    field_types = list(coll.aggregate(pipeline))

    schema_snapshot = {
        "collection_name": collection_name,
        "snapshot_at":     datetime.now(timezone.utc),
        "fields": [
            {
                "name":  f["_id"],
                "types": f["types"],
                "count": f["count"],
            }
            for f in field_types
        ],
    }

    app_db.schema_snapshots.insert_one(schema_snapshot)
    logger.info("  Schema snapshot saved: %d fields tracked.", len(field_types))


def main() -> None:
    logger.info("Connecting to MongoDB…")
    client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10_000)
    source_db = client[SOURCE_DB]
    app_db    = client["querysentinel"]

    # Verify connection
    try:
        client.admin.command("ping")
        logger.info("MongoDB connected ✓")
    except Exception as e:
        logger.error("Cannot reach MongoDB: %s", e)
        sys.exit(1)

    logger.info("")
    logger.info("Computing baselines for: %s", MONITORED_COLLECTIONS)
    logger.info("")

    summaries = []

    for cname in MONITORED_COLLECTIONS:
        logger.info("── %s ──", cname)

        try:
            baseline = _compute_single_baseline(source_db, app_db, cname)
            if not baseline:
                continue

            mean_kb    = round(baseline["mean_bytes"] / 1024, 2)
            stddev_kb  = round(baseline["stddev_bytes"] / 1024, 2)
            p99_kb     = round((baseline["p99_bytes"] or 0) / 1024, 2)
            warn_kb    = round(baseline["warning_threshold_bytes"] / 1024, 2)
            crit_kb    = round(baseline["critical_threshold_bytes"] / 1024, 2)

            logger.info("  Docs sampled:   %d (est. total: %d)", baseline["sample_count"], baseline["doc_count_est"])
            logger.info("  Mean size:      %.2f KB", mean_kb)
            logger.info("  Std dev:        %.2f KB", stddev_kb)
            logger.info("  P99 size:       %.2f KB", p99_kb)
            logger.info("  Warning @:      %.2f KB  (Z ≥ 2.0)", warn_kb)
            logger.info("  Critical @:     %.2f KB  (Z ≥ 3.5)", crit_kb)
            logger.info("  Indexes:        %s", baseline["index_names"])

            _snapshot_schema(source_db, app_db, cname)

            summaries.append({
                "collection":  cname,
                "mean_kb":     mean_kb,
                "warning_kb":  warn_kb,
                "critical_kb": crit_kb,
            })

        except Exception as e:
            logger.error("  Failed to baseline %s: %s", cname, e)

        logger.info("")

    # ── Print summary table ───────────────────────────────────────────────────
    logger.info("═" * 60)
    logger.info("%-20s %10s %12s %12s", "Collection", "Mean (KB)", "Warn @ (KB)", "Crit @ (KB)")
    logger.info("─" * 60)
    for s in summaries:
        logger.info("%-20s %10.2f %12.2f %12.2f", s["collection"], s["mean_kb"], s["warning_kb"], s["critical_kb"])
    logger.info("═" * 60)
    logger.info("")
    logger.info("✓  Baselines stored in app_db.collection_baselines")
    logger.info("✓  Schema snapshots stored in app_db.schema_snapshots")
    logger.info("")
    logger.info("Next steps:")
    logger.info("  1. python seed.py         — seed anomaly_history for vector search")
    logger.info("  2. python main.py          — start the FastAPI backend")


if __name__ == "__main__":
    main()
