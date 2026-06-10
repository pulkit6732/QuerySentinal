"""
setup_asp.py — Create the Atlas Stream Processing processor that feeds
QUERYSENTINEL's runway prediction (Bug 3 + Bug 5 fix).

What this fixes:
  - Bug 3: $group._id was "$collection_name" → overwrote the same doc every
           window, so stream_stats had 1 row per collection forever.
           Slope = 0, trend = "stable", runway = "insufficient data" forever.
  - Bug 5: There was no setup script at all, so ASP was never wired up.

The correct compound _id is:
    _id: { collection: "$collection_name", window_end: "$$WINDOW.end" }

This appends one document per (collection, window) so the runway calculator
has a real time-series to compute slope from.

Usage:
    python -m scripts.setup_asp           # idempotent — safe to re-run

Atlas requirements:
  - An ASP instance must already exist in the project (create one in Atlas UI:
    Stream Processing → Create Instance). Set ASP_INSTANCE_NAME to its name.
  - An Atlas service-account API key with Stream Processing privileges.

Auth: uses ATLAS_CLIENT_ID + ATLAS_CLIENT_SECRET from .env (OAuth client_credentials
flow against Atlas Admin API v2).
"""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

import requests

from config import (
    ATLAS_CLIENT_ID,
    ATLAS_CLIENT_SECRET,
    ATLAS_PROJECT_ID,
    APP_DB,
    MONITORED_COLLECTIONS,
    SOURCE_DB,
    ASP_INSTANCE_NAME,
    ASP_PROCESSOR_NAME,
    ASP_CONNECTION_NAME,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("setup_asp")

ATLAS_API_BASE = "https://cloud.mongodb.com/api/atlas/v2"
OAUTH_TOKEN_URL = "https://cloud.mongodb.com/api/oauth/token"


# ── Auth ─────────────────────────────────────────────────────────────────────

def _get_access_token() -> str:
    """OAuth client_credentials → bearer token for Atlas Admin API."""
    if not (ATLAS_CLIENT_ID and ATLAS_CLIENT_SECRET):
        raise SystemExit(
            "ATLAS_CLIENT_ID / ATLAS_CLIENT_SECRET missing. "
            "Create a service account in Atlas → Access Management → API Keys."
        )
    resp = requests.post(
        OAUTH_TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(ATLAS_CLIENT_ID, ATLAS_CLIENT_SECRET),
        headers={"Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.atlas.2024-05-30+json",
        "Content-Type": "application/json",
    }


# ── Pipeline definition (THE FIX for Bug 3) ──────────────────────────────────

def _build_pipeline() -> list[dict[str, Any]]:
    """
    Stream pipeline:
      $source       — Atlas change stream across monitored collections
      $tumblingWindow(60s) →
          $group { _id: {collection, window_end}, write_count, mean_size }
      $merge into APP_DB.stream_stats  (whenMatched: keepExisting, whenNotMatched: insert)

    Why this _id matters:
      - Compound key with $$WINDOW.end means each completed window writes a
        NEW document, not an overwrite. The runway calculator then sees a real
        time series (e.g. 6 docs for the last 6 minutes) and can fit a slope.
      - $$WINDOW is the system variable exposed inside $tumblingWindow's inner
        pipeline (Atlas Stream Processing).
    """
    return [
        {
            "$source": {
                "connectionName": ASP_CONNECTION_NAME,
                "db": SOURCE_DB,
                "coll": list(MONITORED_COLLECTIONS),
                "config": {"fullDocument": "updateLookup"},
            }
        },
        {
            "$tumblingWindow": {
                "interval": {"size": 60, "unit": "second"},
                "pipeline": [
                    {
                        "$group": {
                            # Compound _id ensures one NEW document per (collection, window)
                            "_id": {
                                "collection": "$ns.coll",
                                "window_end": "$$WINDOW.end",
                            },
                            "event_count":  {"$sum": 1},
                            "mean_bytes":   {"$avg": {"$bsonSize": "$fullDocument"}},
                            "max_bytes":    {"$max": {"$bsonSize": "$fullDocument"}},
                            "window_start": {"$first": "$$WINDOW.start"},
                        }
                    }
                ],
            }
        },
        {
            # Hoist _id fields to top level so backend queries are simple:
            #   db.stream_stats.find({collection_name: "X"}).sort({window_end: -1})
            "$set": {
                "collection_name":  "$_id.collection",
                "window_end":       "$_id.window_end",
                "current_rate_per_min": "$event_count",
                "avg_doc_size_kb":  {"$divide": ["$mean_bytes", 1024]},
                "max_doc_size_kb":  {"$divide": ["$max_bytes",  1024]},
            }
        },
        {
            "$merge": {
                "into": {
                    "connectionName": ASP_CONNECTION_NAME,
                    "db":             APP_DB,
                    "coll":           "stream_stats",
                },
                "on": "_id",
                "whenMatched":    "keepExisting",
                "whenNotMatched": "insert",
            }
        },
    ]


# ── Admin API calls ──────────────────────────────────────────────────────────

def _processor_url(instance: str, name: str | None = None) -> str:
    base = (
        f"{ATLAS_API_BASE}/groups/{ATLAS_PROJECT_ID}"
        f"/streams/{instance}/processors"
    )
    return f"{base}/{name}" if name else base


def _get_existing(token: str, instance: str, name: str) -> dict | None:
    resp = requests.get(
        _processor_url(instance, name),
        headers=_auth_headers(token),
        timeout=15,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _create_processor(token: str, instance: str, name: str, pipeline: list) -> dict:
    body = {"name": name, "pipeline": pipeline}
    resp = requests.post(
        _processor_url(instance),
        headers=_auth_headers(token),
        data=json.dumps(body),
        timeout=30,
    )
    if resp.status_code >= 400:
        raise SystemExit(
            f"Create processor failed ({resp.status_code}): {resp.text}"
        )
    return resp.json()


def _delete_processor(token: str, instance: str, name: str) -> None:
    requests.delete(
        _processor_url(instance, name),
        headers=_auth_headers(token),
        timeout=15,
    )


def _start_processor(token: str, instance: str, name: str) -> None:
    resp = requests.post(
        f"{_processor_url(instance, name)}:start",
        headers=_auth_headers(token),
        timeout=30,
    )
    if resp.status_code >= 400:
        raise SystemExit(
            f"Start processor failed ({resp.status_code}): {resp.text}"
        )


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    if not ATLAS_PROJECT_ID:
        logger.error("ATLAS_PROJECT_ID not set in .env")
        return 1

    logger.info("Acquiring Atlas Admin API token…")
    token = _get_access_token()

    logger.info(
        "Checking for existing processor '%s' in instance '%s'…",
        ASP_PROCESSOR_NAME, ASP_INSTANCE_NAME,
    )
    existing = _get_existing(token, ASP_INSTANCE_NAME, ASP_PROCESSOR_NAME)

    if existing:
        logger.info("Found existing processor — deleting for clean recreate.")
        _delete_processor(token, ASP_INSTANCE_NAME, ASP_PROCESSOR_NAME)
        time.sleep(2)

    pipeline = _build_pipeline()
    logger.info("Creating processor with compound _id pipeline…")
    created = _create_processor(token, ASP_INSTANCE_NAME, ASP_PROCESSOR_NAME, pipeline)
    logger.info("Created: %s", created.get("name", ASP_PROCESSOR_NAME))

    logger.info("Starting processor…")
    _start_processor(token, ASP_INSTANCE_NAME, ASP_PROCESSOR_NAME)

    logger.info("")
    logger.info("✓ Atlas Stream Processor '%s' is RUNNING.", ASP_PROCESSOR_NAME)
    logger.info("  Window: 60 seconds, tumbling")
    logger.info("  Source: %s.[%s]", SOURCE_DB, ",".join(MONITORED_COLLECTIONS))
    logger.info("  Sink:   %s.stream_stats", APP_DB)
    logger.info("")
    logger.info("Verify in mongosh:")
    logger.info("  use %s; db.stream_stats.find().sort({_id:-1}).limit(5)", APP_DB)
    logger.info("Expected: one NEW document per (collection, 60s window).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
