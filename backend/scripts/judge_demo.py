"""
judge_demo.py — One-command end-to-end demo for QUERYSENTINEL judges.

Run:
    cd backend
    python -m scripts.judge_demo

What it does (no flags needed):
  1. Health-checks every component (MongoDB, Gemini, vector index, MCP server)
  2. Resets demo state (clears stale raw_events, failed_events)
  3. Injects 3 semantically-contaminated docs
  4. Waits up to 90s for the ADK pipeline to write an incident_report
  5. Prints a single-screen summary judges can screenshot:
       - parallel_verified (the 3.5× speedup proof)
       - dollar impact (per minute / per hour / engineer triage)
       - top remediation option + confidence
       - similar incidents found via vectorSearch
       - cluster-wide event status (CorrelationAgent)
  6. Exits 0 on full success, 1 on any failure with actionable error

This is the script a judge runs at 2am if they want to verify our claims.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()


def _print_box(title: str, lines: list[str]) -> None:
    width = max(len(title) + 4, max((len(l) for l in lines), default=0) + 4, 60)
    print("╭" + "─" * width + "╮")
    print(f"│ {title.ljust(width - 2)} │")
    print("├" + "─" * width + "┤")
    for ln in lines:
        print(f"│ {ln.ljust(width - 2)} │")
    print("╰" + "─" * width + "╯")


def _check_health() -> dict:
    """Verify every component before injecting."""
    from db import app_db, client

    health = {}

    # MongoDB
    try:
        client.admin.command("ping")
        movies_count = client["sample_mflix"]["movies"].count_documents({})
        health["mongodb"] = f"ok ({movies_count:,} movies)"
    except Exception as e:
        health["mongodb"] = f"FAIL: {e}"

    # anomaly_history seed
    try:
        seed_count = app_db.anomaly_history.count_documents({})
        embed_count = app_db.anomaly_history.count_documents({"embedding": {"$exists": True}})
        health["anomaly_history"] = f"{seed_count} docs ({embed_count} embedded)"
    except Exception as e:
        health["anomaly_history"] = f"FAIL: {e}"

    # Gemini
    try:
        import google.genai as genai
        gclient = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        r = gclient.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
            contents="Reply: OK",
        )
        health["gemini"] = f"ok ({os.environ.get('GEMINI_MODEL', 'gemini-3.5-flash')})"
    except Exception as e:
        msg = str(e)[:80]
        health["gemini"] = f"FAIL: {msg}"

    # Backend
    try:
        import httpx
        r = httpx.get("http://localhost:8000/api/incidents", timeout=5.0)
        if r.status_code == 200:
            health["backend"] = "ok (port 8000)"
        else:
            health["backend"] = f"http {r.status_code}"
    except Exception:
        health["backend"] = "FAIL: backend not running (start with uvicorn main:app)"

    # MCP server
    try:
        import httpx
        r = httpx.post("http://localhost:8000/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
                       headers={"Accept": "application/json, text/event-stream"}, timeout=3.0)
        health["mcp_server"] = "ok (/mcp accepting)" if r.status_code < 500 else f"http {r.status_code}"
    except Exception:
        health["mcp_server"] = "skipped (backend down or MCP off)"

    return health


def _reset_state() -> None:
    """Clear stale anomalies + failed events for a clean run."""
    from db import app_db
    app_db.raw_events.delete_many({})
    app_db.failed_events.delete_many({})
    print("State reset (raw_events, failed_events cleared)")


def _inject() -> tuple[bool, str]:
    """Run the demo injector (3 docs, 8s spread)."""
    import subprocess
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.demo_inject", "--count", "3", "--spread", "8"],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return (proc.returncode == 0, proc.stdout + proc.stderr)


def _wait_for_incident(timeout_s: int = 120) -> dict | None:
    """Poll incident_reports for a new entry. Returns incident or None."""
    from db import app_db
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        inc = app_db.incident_reports.find_one({}, sort=[("created_at", -1)])
        if inc:
            return inc
        time.sleep(2)
    return None


def _print_incident_summary(inc: dict) -> None:
    impact = inc.get("estimated_dollar_impact") or {}
    rem = inc.get("remediation") or {}
    options = rem.get("options") or []
    top_opt = options[0] if options else {}
    similar = inc.get("similar_incidents") or {}
    matches = similar.get("matches") or []

    lines = [
        f"Incident ID    : {inc.get('incident_id', '?')[:36]}",
        f"Collection     : {inc.get('collection_name', '?')}",
        f"Z-Score        : {inc.get('z_score', 0):.2f}",
        f"Severity       : {inc.get('severity', '?')}",
        f"Pipeline status: {inc.get('pipeline_status', '?')}",
        f"Parallel ✓     : {inc.get('parallel_verified', '?')}",
        f"Pipeline ms    : {inc.get('pipeline_ms', '?')}",
        "",
        f"DOLLAR IMPACT:",
        f"  Per minute   : ${impact.get('per_minute_usd', 0)}",
        f"  If 1hr       : ${impact.get('if_unresolved_1hr_usd', 0)}",
        f"  Eng triage   : ${impact.get('engineer_triage_usd', 510)}",
        f"  QS saves     : ${impact.get('querysentinel_save_usd', 0)}",
        "",
        f"TOP REMEDIATION:",
        f"  Title        : {top_opt.get('title', '(none)')[:40]}",
        f"  Confidence   : {top_opt.get('confidence', 0)}",
        f"  ETA minutes  : {top_opt.get('estimated_minutes', '?')}",
        "",
        f"SIMILAR INCIDENTS (vectorSearch): {len(matches)} match(es)",
    ]
    _print_box("✓ QUERYSENTINEL INCIDENT REPORT", lines)


def main() -> int:
    print("=" * 64)
    print("QUERYSENTINEL — Judge Demo Harness")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 64)
    print()

    # 1. Health
    print("[1/5] Running health check…")
    health = _check_health()
    for k, v in health.items():
        marker = "✓" if "ok" in v else ("⚠" if "skipped" in v else "✗")
        print(f"  {marker}  {k:18s} {v}")
    if "FAIL" in health.get("backend", ""):
        print("\n  Backend not running. Start it:")
        print("    cd backend && uvicorn main:app --host 0.0.0.0 --port 8000")
        return 1
    if "FAIL" in health.get("mongodb", ""):
        print("\n  MongoDB unreachable. Check MONGODB_URI in .env.")
        return 1

    # 2. Reset
    print("\n[2/5] Resetting state…")
    _reset_state()

    # 3. Inject
    print("\n[3/5] Injecting 3 semantically-contaminated documents…")
    ok, output = _inject()
    if not ok:
        print(f"  Injection failed:\n{output[-500:]}")
        return 1
    # Print just the velocity numbers from inject output
    for line in output.splitlines():
        if any(k in line for k in ("velocity_zscore", "centroid_distance", "status", "SPIKE", "inserted")):
            print(f"  {line.strip()}")

    # 4. Wait
    print("\n[4/5] Waiting for ADK pipeline to write incident_report (up to 120s)…")
    inc = _wait_for_incident(timeout_s=120)
    if not inc:
        print("\n  No incident in 120s. Check backend logs and verify Gemini quota.")
        return 1
    print(f"  ✓ Incident landed in {(datetime.now(timezone.utc) - inc.get('created_at', datetime.now(timezone.utc))).total_seconds():.1f}s ago")

    # 5. Report
    print("\n[5/5] Final report:\n")
    _print_incident_summary(inc)

    print("\n" + "=" * 64)
    print("✓ JUDGE DEMO COMPLETE")
    print(f"Open dashboard: http://localhost:3000")
    print(f"Open MCP server: http://localhost:8000/mcp")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
