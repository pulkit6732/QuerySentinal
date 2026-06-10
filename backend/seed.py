"""
seed.py — Seed 120 realistic anomaly_history documents for QUERYSENTINEL.

Atlas voyage-4 Automated Embedding will auto-embed the 'description' field
when documents are inserted (trigger configured on anomaly_history collection).

Run once before first demo:
    python seed.py

Requirements:
    - .env file with MONGODB_URI
    - anomaly_history collection must have the voyage-4 auto-embedding trigger configured
    - Atlas Search index 'anomaly_semantic' must exist on anomaly_history.embedding
"""
import logging
import random
import sys
from datetime import datetime, timedelta, timezone
from typing import List

from faker import Faker
from pymongo import MongoClient

from config import MONGODB_URI

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("seed")

fake = Faker()

# ── Incident templates (realistic MongoDB production patterns) ────────────────

INCIDENT_TEMPLATES = [
    # DOC_SIZE_SPIKE
    {
        "anomaly_type": "DOC_SIZE_SPIKE",
        "collection_name": "movies",
        "severity": "CRITICAL",
        "description": "Document size spike in movies collection — base64 encoded binary embedded inline instead of GridFS. Mean document size jumped from 4 KB to 698 KB. Z-score 7.2.",
        "root_cause": "Application bug introduced base64 image encoding directly into document body instead of using GridFS or S3 reference URLs.",
        "resolution_steps": "1. Identify offending documents with $bsonSize > 500KB. 2. Extract binary to GridFS. 3. Update documents with GridFS reference. 4. Delete oversized originals. 5. Deploy application fix to use GridFS write path.",
        "resolution_time_minutes": 47,
        "tags": ["binary-inline", "gridfs", "application-bug"],
    },
    {
        "anomaly_type": "DOC_SIZE_SPIKE",
        "collection_name": "sales",
        "severity": "CRITICAL",
        "description": "Unbounded array growth in sales.items field. Documents accumulating all historical line items without pagination. Size grew 3x over 6 hours. Z-score 5.8.",
        "root_cause": "Missing pagination — application appended all order history to single document rather than creating separate order documents per transaction.",
        "resolution_steps": "1. Run $group to identify top offenders by array length. 2. Refactor schema to separate orders collection. 3. Create index on customer_id in orders. 4. Migrate existing data with bulk operations.",
        "resolution_time_minutes": 92,
        "tags": ["unbounded-array", "schema-refactor", "pagination"],
    },
    {
        "anomaly_type": "DOC_SIZE_SPIKE",
        "collection_name": "comments",
        "severity": "WARNING",
        "description": "Audit log array embedded in comments document growing without bound. Each document now contains 2,000+ audit entries. P99 size: 1.2 MB. Z-score 3.1.",
        "root_cause": "Audit logging routed to wrong collection — appending to parent document instead of dedicated audit_log collection.",
        "resolution_steps": "1. Create audit_log collection with TTL index (90 days). 2. Update logging pipeline to write to audit_log. 3. Migrate existing audit arrays using aggregation pipeline + $out.",
        "resolution_time_minutes": 34,
        "tags": ["audit-log", "ttl-index", "schema-refactor"],
    },
    {
        "anomaly_type": "DOC_SIZE_SPIKE",
        "collection_name": "theaters",
        "severity": "CRITICAL",
        "description": "Nested geospatial history array in theaters documents ballooned after GPS polling interval reduced from 1hr to 1sec. Documents averaging 890 KB. Z-score 9.4.",
        "root_cause": "IoT device configuration change — GPS ping frequency increased 3600x without corresponding change to data storage strategy.",
        "resolution_steps": "1. Revert GPS poll interval to 1hr or route high-frequency data to time-series collection. 2. Use Atlas Time Series collection with timeseries.granularity = 'seconds'. 3. Remove embedded history array from theaters documents.",
        "resolution_time_minutes": 23,
        "tags": ["iot", "time-series", "granularity", "gps"],
    },
    # SCHEMA_DRIFT
    {
        "anomaly_type": "SCHEMA_DRIFT",
        "collection_name": "movies",
        "severity": "WARNING",
        "description": "New field 'aiGeneratedSummary' (string, ~800 chars) added to movies collection without schema registry update. 12% of documents now have this field. Polymorphic inconsistency detected.",
        "root_cause": "New ML enrichment pipeline deployed without schema change review. Field added directly in production without staging validation.",
        "resolution_steps": "1. Add 'aiGeneratedSummary' to JSON Schema validation. 2. Create sparse index on aiGeneratedSummary for text search. 3. Backfill null for documents missing the field. 4. Update schema registry.",
        "resolution_time_minutes": 28,
        "tags": ["schema-drift", "json-validation", "ml-enrichment"],
    },
    {
        "anomaly_type": "SCHEMA_DRIFT",
        "collection_name": "sales",
        "severity": "CRITICAL",
        "description": "Field 'price' type changed from Double to String in 31% of new documents. Aggregation queries using $sum now return incorrect results. Revenue reporting affected.",
        "root_cause": "New payment processor integration sends price as string '19.99' instead of numeric 19.99. No input validation in API layer.",
        "resolution_steps": "1. Add $type validation to JSON Schema for price field. 2. Run migration: db.sales.updateMany({'price': {$type: 'string'}}, [{$set: {price: {$toDouble: '$price'}}}]). 3. Fix payment processor API adapter.",
        "resolution_time_minutes": 61,
        "tags": ["type-coercion", "json-schema", "payment-integration"],
    },
    {
        "anomaly_type": "SCHEMA_DRIFT",
        "collection_name": "users",
        "severity": "WARNING",
        "description": "15 previously required fields now absent in 8% of documents. Authentication pipeline relying on these fields now throwing NullPointerExceptions in application layer.",
        "root_cause": "Database migration script had incorrect $unset operation — removed fields from wrong document subset.",
        "resolution_steps": "1. Identify affected documents with $exists: false on required fields. 2. Restore from backup for those specific documents. 3. Add JSON Schema validation to enforce required fields. 4. Audit migration script.",
        "resolution_time_minutes": 85,
        "tags": ["migration-error", "required-fields", "json-schema"],
    },
    {
        "anomaly_type": "SCHEMA_DRIFT",
        "collection_name": "comments",
        "severity": "WARNING",
        "description": "Array field 'tags' changed to comma-separated string in 23% of documents from new mobile client. Atlas Search index on tags now returning degraded results.",
        "root_cause": "Mobile app v2.3.1 serializing tags as string instead of array. Backend API accepted both without validation.",
        "resolution_steps": "1. Normalize: updateMany where tags is string → {$set: {tags: {$split: ['$tags', ',']}}}. 2. Add JSON Schema enum validation. 3. Fix mobile app serializer in v2.3.2 hotfix.",
        "resolution_time_minutes": 19,
        "tags": ["array-string-coercion", "mobile-client", "atlas-search"],
    },
    # WRITE_VOLUME_SPIKE
    {
        "anomaly_type": "WRITE_VOLUME_SPIKE",
        "collection_name": "sales",
        "severity": "CRITICAL",
        "description": "Write volume spike: 47,000 writes/min on sales collection vs baseline 1,200/min. Atlas Stream Processing detected 39x spike. Replication lag growing. Z-score 8.1.",
        "root_cause": "Retry storm from message queue — dead letter queue processor retrying 500k failed transactions without exponential backoff.",
        "resolution_steps": "1. Immediately: db.adminCommand({setParameter: 1, maxWritesPerTx: 500}) to throttle. 2. Pause DLQ processor. 3. Add exponential backoff with jitter (max 5 min). 4. Process backlog at 5k writes/min.",
        "resolution_time_minutes": 18,
        "tags": ["retry-storm", "dlq", "backoff", "replication-lag"],
    },
    {
        "anomaly_type": "WRITE_VOLUME_SPIKE",
        "collection_name": "movies",
        "severity": "WARNING",
        "description": "Bulk import job running during peak hours. 2M documents inserted over 45 minutes. Write concern w:1 causing oplog pressure on secondaries.",
        "root_cause": "Scheduled bulk import not restricted to off-peak hours. No rate limiting on import pipeline.",
        "resolution_steps": "1. Pause import job. 2. Reschedule to 2-4 AM UTC. 3. Add rate limiter: 10k docs/min max. 4. Use ordered:false for parallel insert performance.",
        "resolution_time_minutes": 12,
        "tags": ["bulk-import", "oplog-pressure", "scheduling"],
    },
    {
        "anomaly_type": "WRITE_VOLUME_SPIKE",
        "collection_name": "sessions",
        "severity": "CRITICAL",
        "description": "Session collection write storm from authentication microservice. Every API call creating new session document instead of updating existing. 200k writes/min.",
        "root_cause": "Session ID cookie not being sent from load balancer — sticky sessions misconfigured after infrastructure change.",
        "resolution_steps": "1. Fix load balancer sticky session config. 2. Enable upsert on session writes. 3. Add TTL index (24h) to sessions collection. 4. Monitor write rate via Atlas Stream Processing.",
        "resolution_time_minutes": 7,
        "tags": ["session-storm", "load-balancer", "ttl-index", "auth"],
    },
    # SEMANTIC_VELOCITY_SPIKE
    {
        "anomaly_type": "SEMANTIC_VELOCITY_SPIKE",
        "collection_name": "movies",
        "severity": "WARNING",
        "description": "Semantic velocity spike in movies collection: cosine distance between hourly embedding centroids jumped to 0.34 (baseline: 0.04). Content categorization semantics drifting. Possible data quality issue.",
        "root_cause": "New content tagging model deployed with different embedding space calibration. Semantic drift indicates model mismatch, not actual content change.",
        "resolution_steps": "1. Roll back content tagging model to v1.2. 2. Re-embed affected documents with consistent model. 3. Update Atlas Automated Embedding trigger to pin model version. 4. Monitor centroid distance daily.",
        "resolution_time_minutes": 55,
        "tags": ["embedding-drift", "model-mismatch", "semantic-velocity"],
    },
    {
        "anomaly_type": "SEMANTIC_VELOCITY_SPIKE",
        "collection_name": "comments",
        "severity": "CRITICAL",
        "description": "Extreme semantic velocity in comments collection: centroid drift 0.71 over 2 hours. Comment text content fundamentally changed — likely spam injection or bot activity detected.",
        "root_cause": "Spam bot campaign inserting off-topic commercial content. 18,000 spam comments inserted before detection. Semantic centroid moved from entertainment to financial spam.",
        "resolution_steps": "1. Delete documents matching spam pattern: {text: /\b(crypto|forex|investment)\b/i, created_at: {$gte: <window_start>}}. 2. Block IPs via Atlas App Services. 3. Enable CAPTCHA on comment endpoint. 4. Rebuild semantic index.",
        "resolution_time_minutes": 38,
        "tags": ["spam-injection", "bot-activity", "semantic-drift", "content-moderation"],
    },
    {
        "anomaly_type": "SEMANTIC_VELOCITY_SPIKE",
        "collection_name": "theaters",
        "severity": "WARNING",
        "description": "Semantic velocity spike in theater descriptions: centroid drift 0.28. New 'virtual venue' category introduced without gradual rollout. Embedding space shifted significantly.",
        "root_cause": "New product category (virtual/online theaters) launched with semantically distinct description patterns. Expected drift but no monitoring alert configured.",
        "resolution_steps": "1. Acknowledge as expected product launch drift. 2. Update semantic velocity baseline window to exclude launch period. 3. Document expected drift magnitude for future launches.",
        "resolution_time_minutes": 8,
        "tags": ["product-launch", "category-expansion", "baseline-update"],
    },
]

# ── Generate 120 synthetic incidents from templates ───────────────────────────

def generate_incidents(n: int = 120) -> List[dict]:
    incidents = []
    base_time = datetime.now(timezone.utc) - timedelta(days=90)

    for i in range(n):
        template = random.choice(INCIDENT_TEMPLATES)

        # Random time in last 90 days with realistic clustering
        hours_ago = random.expovariate(1 / 400)  # exponential — more recent incidents more common
        hours_ago = min(hours_ago, 2160)  # cap at 90 days
        incident_time = datetime.now(timezone.utc) - timedelta(hours=hours_ago)

        # Vary z_score with realistic distribution
        if template["severity"] == "CRITICAL":
            z_score = round(random.uniform(3.5, 11.0), 2)
        else:
            z_score = round(random.uniform(2.0, 3.5), 2)

        # Vary resolution time ±30%
        base_resolution = template["resolution_time_minutes"]
        resolution_minutes = int(base_resolution * random.uniform(0.7, 1.3))

        doc = {
            "anomaly_type":            template["anomaly_type"],
            "collection_name":         template["collection_name"],
            "severity":                template["severity"],
            "description":             template["description"],     # voyage-4 will embed this
            "root_cause":              template["root_cause"],
            "resolution_steps":        template["resolution_steps"],
            "resolution_time_minutes": resolution_minutes,
            "z_score":                 z_score,
            "tags":                    template["tags"],
            "created_at":              incident_time,
            "resolved_at":             incident_time + timedelta(minutes=resolution_minutes),
            "status":                  "resolved",
            "seed_doc":                True,   # mark as seeded for easy cleanup
        }

        # Add slight description variation to avoid exact duplicates
        if i % 3 == 0:
            doc["description"] += f" Occurred on {fake.day_of_week()}."
        elif i % 3 == 1:
            doc["description"] += f" Affected approximately {random.randint(100, 50000)} documents."

        incidents.append(doc)

    return incidents


# ── Atlas Vector Search index spec (Bug 6 fix) ────────────────────────────────
# The $vectorSearch in SimilarIncidentAgent fails with a collection-scan error
# if this index doesn't exist. Previously seed.py never created it — agents
# silently returned 0 matches and the 30% vector-search confidence weight
# was always zero. createSearchIndexes is idempotent (errors if exists).

VECTOR_INDEX_NAME = "anomaly_semantic"

VECTOR_INDEX_DEFINITION = {
    "name": VECTOR_INDEX_NAME,
    "type": "vectorSearch",
    "definition": {
        "fields": [
            {
                "type":          "vector",
                "path":          "embedding",
                "numDimensions": 1024,        # voyage-4 output dimension
                "similarity":    "cosine",
            },
            # Filter fields enable pre-filtered vector search
            {"type": "filter", "path": "collection_name"},
            {"type": "filter", "path": "anomaly_type"},
            {"type": "filter", "path": "severity"},
        ]
    },
}


def ensure_vector_index(db) -> None:
    """
    Create the anomaly_semantic vector search index if it doesn't exist.
    Atlas Search indexes are created asynchronously — the call returns
    immediately but the index takes 1–3 minutes to become queryable.
    """
    try:
        existing = list(db.anomaly_history.list_search_indexes(name=VECTOR_INDEX_NAME))
        if existing:
            logger.info("Vector index '%s' already exists. Skipping create.", VECTOR_INDEX_NAME)
            return
    except Exception as e:
        # Older drivers don't have list_search_indexes; fall through to create
        logger.debug("list_search_indexes unavailable (%s); attempting create.", e)

    try:
        db.command(
            "createSearchIndexes",
            "anomaly_history",
            indexes=[VECTOR_INDEX_DEFINITION],
        )
        logger.info("✓ Created Atlas Vector Search index '%s'.", VECTOR_INDEX_NAME)
        logger.info("  Index build is async — fully queryable in ~1–3 minutes.")
        logger.info("  Check status: db.anomaly_history.aggregate([{\"$listSearchIndexes\":{}}])")
    except Exception as e:
        msg = str(e).lower()
        if "already exists" in msg or "indexalreadyexists" in msg or "duplicatekey" in msg:
            logger.info("Vector index '%s' already exists.", VECTOR_INDEX_NAME)
        else:
            logger.error("Could not create vector index '%s': %s", VECTOR_INDEX_NAME, e)
            logger.error("Vector search will NOT work until this index exists. Create it manually:")
            logger.error("  Atlas UI → Search → Create Search Index → JSON Editor")
            logger.error("  Database: querysentinel  Collection: anomaly_history")
            logger.error("  Index name: %s", VECTOR_INDEX_NAME)


def main() -> None:
    logger.info("Connecting to MongoDB…")
    client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10_000)
    db = client["querysentinel"]

    # ── Always ensure the vector index exists (even on re-runs) ───────────────
    ensure_vector_index(db)

    # Check if already seeded
    existing = db.anomaly_history.count_documents({"seed_doc": True})
    if existing >= 100:
        logger.info("Already seeded (%d seed docs found). Skipping inserts.", existing)
        logger.info("To re-seed: db.anomaly_history.deleteMany({seed_doc: true})")
        return

    logger.info("Generating 120 synthetic anomaly history incidents…")
    incidents = generate_incidents(120)

    logger.info("Inserting to anomaly_history…")
    result = db.anomaly_history.insert_many(incidents, ordered=False)
    logger.info("Inserted %d documents.", len(result.inserted_ids))

    logger.info("")
    logger.info("✓  Seed complete. voyage-4 Automated Embedding is now generating vectors.")
    logger.info("   This may take 30–60 seconds depending on Atlas trigger processing time.")
    logger.info("")
    logger.info("   Verify embeddings are generated:")
    logger.info("   db.anomaly_history.findOne({embedding: {$exists: true}}).embedding.length")
    logger.info("   Expected: 1024")
    logger.info("")
    logger.info("   Check all embedded:")
    logger.info("   db.anomaly_history.countDocuments({embedding: {$exists: true}})")
    logger.info("   Expected: 120")
    logger.info("")
    logger.info("   Vector search index '%s' is building (async, ~1–3 min).", VECTOR_INDEX_NAME)
    logger.info("   Once READY, the SimilarIncidentAgent will return real matches.")


if __name__ == "__main__":
    main()
