"""
mcp_server.py — QUERYSENTINEL as an MCP Server.

QUERYSENTINEL doesn't just CONSUME MongoDB MCP tools.
It EXPOSES its own anomaly-intelligence as MCP tools for other ADK agents,
and any MCP-compatible client.

This is the architectural inversion that no other submission has:
  "We ARE an MCP server. Other agents call us."

Tools exposed (5 tools):
  detect_anomaly         — trigger semantic velocity detection on a collection
  list_incidents         — get recent incidents with remediation options
  get_semantic_velocity  — current cosine drift velocity + Z-score
  get_similar_incidents  — $vectorSearch over incident history (voyage-4)
  get_cluster_events     — recent CLUSTER_WIDE_EVENT correlated anomalies

Transport: Streamable HTTP at /mcp (mcp>=1.9.0 FastMCP.streamable_http_app())
MCP client config: Add { "querysentinel": { "url": "http://localhost:8000/mcp" } }
ADK MCPToolset: MCPToolset(connection_params=StreamableHTTPServerParams(url="...url.../mcp"))

Usage (mount in FastAPI main.py):
    from mcp_server import qs_mcp
    app.mount("/mcp", qs_mcp.streamable_http_app())   # mcp >= 1.9.0
"""

import json
import logging
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from config import MONITORED_COLLECTIONS
from db import app_db

logger = logging.getLogger("mcp_server")

# ── MCP server instance ───────────────────────────────────────────────────────

qs_mcp = FastMCP(
    name="querysentinel",
    instructions=(
        "QuerySentinel is a live MongoDB anomaly-intelligence system. "
        "Use detect_anomaly to check a collection for semantic drift or size anomalies. "
        "Use list_incidents to retrieve recent incidents with AI-generated root causes and ranked remediations. "
        "Use get_semantic_velocity to measure voyage-4 cosine centroid drift in a collection. "
        "Use get_similar_incidents to vector-search historical incident memory. "
        "Use get_cluster_events to check for correlated multi-collection cluster events."
    ),
)


# ── Tool: detect_anomaly ──────────────────────────────────────────────────────

@qs_mcp.tool()
async def detect_anomaly(collection_name: str) -> str:
    """
    Run real-time semantic anomaly detection on a MongoDB collection.

    Computes the current semantic velocity (cosine centroid drift between
    consecutive hourly voyage-4 embedding centroids). Returns Z-score,
    drift distance, and whether a spike incident pipeline was triggered.

    Args:
        collection_name: MongoDB collection to check. Monitored collections:
                         movies, comments, users, sessions (or any in MONITORED_COLLECTIONS).
    """
    if collection_name not in MONITORED_COLLECTIONS:
        return json.dumps({
            "error":     f"'{collection_name}' is not monitored.",
            "monitored": MONITORED_COLLECTIONS,
        })

    try:
        from detect import compute_semantic_velocity
        sv = compute_semantic_velocity(collection_name)
    except Exception as e:
        return json.dumps({"error": str(e)})

    return json.dumps({
        "collection":     collection_name,
        "z_score":        round(sv.get("velocity_zscore", 0.0), 2),
        "drift_distance": round(sv.get("centroid_distance", 0.0), 4),
        "baseline":       round(sv.get("baseline_distance", 0.0), 4),
        "status":         sv.get("status", "unknown"),
        "message": (
            "⚠ SPIKE: semantic contamination detected — incident pipeline triggered."
            if sv.get("status") == "spike"
            else "✓ Normal: no semantic anomaly detected."
        ),
        "interpretation": sv.get("interpretation", ""),
        "checked_at":     datetime.now(timezone.utc).isoformat(),
    }, default=str)


# ── Tool: list_incidents ──────────────────────────────────────────────────────

@qs_mcp.tool()
async def list_incidents(limit: int = 10) -> str:
    """
    Return the most recent anomaly incidents detected by QuerySentinel.

    Each incident includes: collection, anomaly type, severity, Z-score,
    AI-generated summary, dollar impact estimate, and top remediation option.

    Args:
        limit: Number of incidents to return (max 20, default 10).
    """
    docs = list(
        app_db.incident_reports.find(
            {},
            {
                "incident_id": 1, "collection_name": 1,
                "anomaly_type": 1, "severity": 1, "z_score": 1,
                "created_at": 1, "summary": 1,
                "estimated_dollar_impact": 1,
                "remediation": 1, "_id": 0,
            },
            sort=[("created_at", -1)],
            limit=min(limit, 20),
        )
    )
    for d in docs:
        if isinstance(d.get("created_at"), datetime):
            d["created_at"] = d["created_at"].isoformat()
        # Trim remediation to just the top option for readability
        remed = d.get("remediation") or {}
        options = remed.get("options") or []
        d["top_remediation"] = options[0] if options else None
        d.pop("remediation", None)

    return json.dumps({"incidents": docs, "count": len(docs)}, default=str)


# ── Tool: get_semantic_velocity ───────────────────────────────────────────────

@qs_mcp.tool()
async def get_semantic_velocity(collection_name: str) -> str:
    """
    Get the current semantic velocity for a MongoDB collection.

    Semantic velocity = cosine distance between this hour's voyage-4 embedding
    centroid and last hour's centroid. Baseline: ~0.03–0.05 units/hour.
    A spike (Z-score > 3) means semantically alien documents are entering the collection
    — LLM-generated noise, ETL misconfiguration, or a data poisoning attack.

    Args:
        collection_name: MongoDB collection to measure.
    """
    try:
        from detect import compute_semantic_velocity
        result = compute_semantic_velocity(collection_name)
        return json.dumps(result, default=str)
    except Exception as e:
        return json.dumps({"error": str(e), "collection": collection_name})


# ── Tool: get_similar_incidents ───────────────────────────────────────────────

@qs_mcp.tool()
async def get_similar_incidents(description: str, limit: int = 3) -> str:
    """
    Vector-search QuerySentinel's incident history for past incidents similar
    to the given description. Uses Atlas $vectorSearch with voyage-4 Automated
    Embeddings — Atlas handles the query embedding automatically.

    Useful for finding historical precedent before approving a remediation option.

    Args:
        description: Free-text description of the incident or anomaly type.
        limit:       Number of matches to return (max 5, default 3).
    """
    pipeline = [
        {"$vectorSearch": {
            "index":         "anomaly_semantic",
            "query":         description,
            "path":          "embedding",
            "numCandidates": 50,
            "limit":         min(limit, 5),
        }},
        {"$project": {
            "incident_id":   1,
            "collection_name": 1,
            "anomaly_type":  1,
            "summary":       1,
            "severity":      1,
            "created_at":    1,
            "score": {"$meta": "vectorSearchScore"},
            "_id": 0,
        }},
    ]
    try:
        results = list(app_db.incident_reports.aggregate(pipeline))
        for r in results:
            if isinstance(r.get("created_at"), datetime):
                r["created_at"] = r["created_at"].isoformat()
        return json.dumps({"matches": results, "count": len(results)}, default=str)
    except Exception as e:
        return json.dumps({"error": str(e), "tip": "Vector search index may not be ready."})


# ── Tool: get_cluster_events ──────────────────────────────────────────────────

@qs_mcp.tool()
async def get_cluster_events(limit: int = 5) -> str:
    """
    Get recent CLUSTER_WIDE_EVENT incidents — coordinated multi-collection anomalies.

    A cluster event fires when 2+ collections show anomalies within the same
    5-minute window, suggesting a coordinated ETL error, LLM content flood,
    or data poisoning attack. Each event includes a confidence score and
    hypothesis generated by CorrelationAgent (6th ADK agent).

    Args:
        limit: Number of cluster events to return (max 10, default 5).
    """
    docs = list(
        app_db.correlated_incidents.find(
            {},
            {
                "correlation_id":       1,
                "collections_affected": 1,
                "hypothesis":           1,
                "attack_pattern":       1,
                "confidence":           1,
                "severity":             1,
                "patient_zero":         1,
                "recommended_action":   1,
                "created_at":           1,
                "_id": 0,
            },
            sort=[("created_at", -1)],
            limit=min(limit, 10),
        )
    )
    for d in docs:
        if isinstance(d.get("created_at"), datetime):
            d["created_at"] = d["created_at"].isoformat()
    return json.dumps({"cluster_events": docs, "count": len(docs)}, default=str)
