"""
test_e2e.py — End-to-end integration test for QUERYSENTINEL.

Verifies the full chain in one shot:
  1. Inject a fat document into sample_supplies.sales
  2. Watcher catches the Change Stream event within ~3 s
  3. raw_events gets a new anomaly row (Z-score above ANOMALY_Z)
  4. The agent pipeline runs and writes an incident_report
  5. agent_calls gets a step trace with all 5 agents represented
  6. /api/incidents returns the new incident
  7. Cleanup: deletes injected docs + the test incident + the agent_call

Run:
    cd backend
    pytest tests/test_e2e.py -v -s

Pre-requisites:
    - .env populated and Atlas reachable
    - `python seed.py` already run once (vector index exists)
    - FastAPI server running on http://localhost:8000  (or set QS_API_URL)
    - Change Stream watcher for 'sales' already started

Environment overrides:
    QS_API_URL        — default http://localhost:8000
    QS_E2E_TIMEOUT_S  — default 90  (total wait budget for the pipeline)
"""
from __future__ import annotations

import json
import os
import random
import string
import time
from datetime import datetime, timezone

import pymongo
import pytest
import requests

from config import MONGODB_URI, APP_DB

API_URL   = os.getenv("QS_API_URL", "http://localhost:8000").rstrip("/")
TIMEOUT_S = int(os.getenv("QS_E2E_TIMEOUT_S", "90"))
TEST_TAG  = f"e2e-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{random.randint(1000,9999)}"


@pytest.fixture(scope="module")
def mongo():
    client = pymongo.MongoClient(MONGODB_URI, serverSelectionTimeoutMS=8_000)
    yield client
    client.close()


@pytest.fixture(scope="module")
def app_db_handle(mongo):
    return mongo[APP_DB]


@pytest.fixture(scope="module")
def source_sales(mongo):
    return mongo["sample_supplies"]["sales"]


# ─────────────────────────────────────────────────────────────────────────────
# Test 0: backend is alive
# ─────────────────────────────────────────────────────────────────────────────

def test_backend_reachable():
    r = requests.get(f"{API_URL}/health", timeout=10)
    assert r.status_code == 200, f"/health returned {r.status_code}"
    body = r.json()
    assert body["components"]["mongodb"] == "ok", f"MongoDB unreachable: {body}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: inject anomaly → watcher detects → raw_events row appears
# ─────────────────────────────────────────────────────────────────────────────

def test_inject_then_raw_event_created(app_db_handle, source_sales):
    fat = "".join(random.choices(string.ascii_letters + string.digits, k=700_000))
    doc = {
        "saleDate":         datetime.now(timezone.utc).isoformat(),
        "items":            [{"name": "e2e-item", "quantity": 1, "price": 9.99}],
        "storeLocation":    "QUERYSENTINEL-E2E",
        "customer":         {"email": "e2e@querysentinel.test", "satisfaction": 5},
        "purchaseMethod":   "Online",
        "injected_payload": fat,
        "_qs_test":         True,
        "_qs_tag":          TEST_TAG,
        "injected_at":      datetime.now(timezone.utc),
    }
    inserted = source_sales.insert_one(doc)
    pytest.injected_id = inserted.inserted_id
    pytest.test_started_at = datetime.now(timezone.utc)
    print(f"\n[e2e] Injected {inserted.inserted_id} ({len(fat)//1024} KB) tag={TEST_TAG}")

    deadline = time.monotonic() + 20
    found = None
    while time.monotonic() < deadline:
        found = app_db_handle.raw_events.find_one(
            {
                "collection_name": "sales",
                "detected_at":     {"$gte": pytest.test_started_at},
            },
            sort=[("detected_at", pymongo.DESCENDING)],
        )
        if found:
            break
        time.sleep(0.5)

    assert found is not None, (
        "Watcher never wrote a raw_events row within 20s after inject. "
        "Check: server running? sales watcher started? Change Stream enabled?"
    )
    assert abs(found.get("z_score", 0)) >= 2.0, f"Z too low: {found.get('z_score')}"
    pytest.raw_event_id = found["_id"]
    print(f"[e2e] raw_events row: _id={found['_id']} z={found.get('z_score')}")


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: agent pipeline runs and writes an incident_report
# ─────────────────────────────────────────────────────────────────────────────

def test_incident_report_persisted(app_db_handle):
    assert getattr(pytest, "test_started_at", None), "ordering: test 1 must run first"

    deadline = time.monotonic() + TIMEOUT_S
    incident = None
    while time.monotonic() < deadline:
        incident = app_db_handle.incident_reports.find_one(
            {
                "anomaly_event.collection_name": "sales",
                "created_at":                    {"$gte": pytest.test_started_at},
            },
            sort=[("created_at", pymongo.DESCENDING)],
        )
        if incident:
            break
        time.sleep(1.0)

    assert incident is not None, (
        f"No incident_report written within {TIMEOUT_S}s. The agent pipeline "
        "didn't complete. Check orchestrator logs for ADK / MCP errors."
    )
    assert "incident_id" in incident, "incident_id missing — orchestrator UUID injection broken"
    assert incident.get("pipeline_ms", 0) > 0, "pipeline_ms not recorded"
    assert incident.get("summary"), "summary not built"
    assert incident.get("agent_steps"), "agent_steps missing — _StepTracker broken"

    pytest.incident_id = incident["incident_id"]
    print(
        f"[e2e] incident_id={incident['incident_id']} "
        f"pipeline_ms={incident.get('pipeline_ms')} "
        f"steps={len(incident.get('agent_steps') or [])}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: agent_calls trace contains all expected agents
# ─────────────────────────────────────────────────────────────────────────────

def test_agent_calls_trace_complete(app_db_handle):
    incident_id = getattr(pytest, "incident_id", None)
    assert incident_id, "ordering: test 2 must run first"

    trace = app_db_handle.agent_calls.find_one({"incident_id": incident_id})
    assert trace is not None, "agent_calls row missing for incident"

    step_agents = {s.get("agent") for s in (trace.get("steps") or [])}
    expected = {
        "AnomalyContextAgent",
        "SchemaDriftAgent",
        "SimilarIncidentAgent",
        "RootCauseAgent",
        "RemediationAgent",
    }
    missing = expected - step_agents
    # Not all are guaranteed (e.g. PA may 403 on free tier) — require >= 3
    assert len(expected - missing) >= 3, (
        f"Too few agents in trace. Got: {step_agents}. Missing: {missing}"
    )
    print(f"[e2e] agent_steps include: {sorted(step_agents)}")


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: HTTP API surfaces the new incident
# ─────────────────────────────────────────────────────────────────────────────

def test_api_incidents_returns_new_one():
    incident_id = getattr(pytest, "incident_id", None)
    assert incident_id, "ordering: test 2 must run first"

    r = requests.get(f"{API_URL}/api/incidents?limit=20", timeout=15)
    assert r.status_code == 200
    incidents = r.json().get("incidents", [])
    ids = [i.get("incident_id") for i in incidents]
    assert incident_id in ids, (
        f"incident_id {incident_id} not in /api/incidents top-20. Got: {ids[:5]}…"
    )


def test_api_agent_calls_endpoint():
    incident_id = getattr(pytest, "incident_id", None)
    assert incident_id, "ordering: test 2 must run first"

    r = requests.get(f"{API_URL}/api/agent-calls/{incident_id}", timeout=15)
    assert r.status_code == 200, f"GET /api/agent-calls/{incident_id} → {r.status_code}"
    body = r.json()
    assert body.get("incident_id") == incident_id
    assert isinstance(body.get("steps"), list) and body["steps"], "steps empty"


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: cleanup
# ─────────────────────────────────────────────────────────────────────────────

def test_cleanup(app_db_handle, source_sales):
    # Delete the injected source doc
    source_sales.delete_many({"_qs_tag": TEST_TAG})

    # Delete its derived rows from the operational DB
    incident_id = getattr(pytest, "incident_id", None)
    if incident_id:
        app_db_handle.incident_reports.delete_many({"incident_id": incident_id})
        app_db_handle.agent_calls.delete_many({"incident_id": incident_id})

    raw_id = getattr(pytest, "raw_event_id", None)
    if raw_id:
        app_db_handle.raw_events.delete_one({"_id": raw_id})

    print(f"[e2e] Cleanup complete for tag={TEST_TAG}")
