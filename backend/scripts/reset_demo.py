"""
reset_demo.py — Clean slate for a fresh demo recording.

Clears the operational collections that accumulate across runs (old anomalies,
incidents, history, failed events) so the dashboard isn't cluttered with
"2 days ago" entries and the System Status shows all-green.

Does NOT touch the source data (sample_mflix) or collection_baselines.

Run before recording:   python -m scripts.reset_demo
Then seed + inject:      python -m scripts.demo_inject
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from db import app_db

# Collections wiped for a clean demo (operational state, regenerated each run)
_RESET = [
    "raw_events",          # live anomaly feed source
    "incident_reports",    # incident modals
    "anomaly_history",     # semantic velocity + similar-incident source (re-seeded by demo_inject)
    "agent_calls",         # agent timing
    "failed_events",       # DLQ — stale entries trip the "Degraded" badge
    "correlated_incidents",
    "tool_audit_log",
]


def main():
    print("Resetting QuerySentinel demo state...")
    for name in _RESET:
        try:
            n = app_db[name].count_documents({})
            app_db[name].delete_many({})
            print(f"  cleared {name:22s} ({n} docs)")
        except Exception as e:
            print(f"  skip    {name:22s} ({e})")
    # Reset stream resume tokens so the watcher starts fresh (optional but clean)
    try:
        app_db.stream_state.delete_many({})
        print("  cleared stream_state (resume tokens reset)")
    except Exception:
        pass
    print("\nDone. Now run:  python -m scripts.demo_inject")
    print("(Restart the backend first if it's running, so it rebuilds the health card.)")


if __name__ == "__main__":
    main()
