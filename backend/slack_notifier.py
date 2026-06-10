"""
slack_notifier.py — Optional Slack webhook output for QUERYSENTINEL incidents.

Production-grade observability path: every incident is also posted to a Slack
channel via incoming webhook. The webhook URL is configured via SLACK_WEBHOOK_URL
in .env. If not set, this is a complete no-op — keeps the rest of the system
working out of the box.

This is what closes the "daily-use stickiness" story for ops teams:
  - Incident fires in MongoDB → Slack message in oncall channel in <1s
  - Slack message includes the dollar impact, top remediation, APPROVE link
  - Engineer can act without opening the dashboard

Format: rich Slack Block Kit with sections, fields, and an APPROVE button.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("slack_notifier")

_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "").strip()
_DASHBOARD_URL = os.getenv("DASHBOARD_PUBLIC_URL", "http://localhost:3000").rstrip("/")


def is_configured() -> bool:
    """True if SLACK_WEBHOOK_URL is set. Used by health endpoints."""
    return bool(_WEBHOOK_URL) and _WEBHOOK_URL.startswith("https://")


async def post_incident(incident_report: dict) -> Optional[dict]:
    """
    Post an incident summary to Slack. Non-blocking failure mode:
    returns None if webhook is not configured or posting fails.

    Args:
        incident_report: The full incident_report dict from orchestrator.

    Returns:
        Slack response dict on success, None on failure or no-op.
    """
    if not is_configured():
        return None

    severity = incident_report.get("severity", "WARNING")
    coll     = incident_report.get("collection_name", "?")
    z_score  = incident_report.get("z_score", 0)
    impact   = incident_report.get("estimated_dollar_impact", {}) or {}
    summary  = incident_report.get("summary", "")[:300]
    incident_id = incident_report.get("incident_id", "")
    remediation  = incident_report.get("remediation", {}) or {}
    top_option   = (remediation.get("options") or [{}])[0]

    color = "#dc2626" if severity == "CRITICAL" else "#f59e0b"
    icon  = "🔴" if severity == "CRITICAL" else "🟡"

    per_min  = impact.get("per_minute_usd", 0)
    one_hour = impact.get("if_unresolved_1hr_usd", 0)
    headline = impact.get("headline") or f"${per_min}/min · ${one_hour} if unresolved 1hr"

    payload = {
        "attachments": [{
            "color":  color,
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text",
                             "text": f"{icon} {severity}: {coll} · Z={z_score:.1f}"},
                },
                {
                    "type":   "section",
                    "text":   {"type": "mrkdwn", "text": f"*Summary:* {summary}"},
                },
                {
                    "type":   "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Cost:* {headline}"},
                        {"type": "mrkdwn", "text": f"*Top remediation:* {top_option.get('title', 'See dashboard')}"},
                        {"type": "mrkdwn", "text": f"*Confidence:* {top_option.get('confidence', 0):.0%}"},
                        {"type": "mrkdwn", "text": f"*ETA to fix:* {top_option.get('estimated_minutes', '?')} min"},
                    ],
                },
                {
                    "type": "actions",
                    "elements": [{
                        "type":  "button",
                        "text":  {"type": "plain_text", "text": "Open in QuerySentinel"},
                        "url":   f"{_DASHBOARD_URL}/incidents/{incident_id}",
                        "style": "primary",
                    }],
                },
                {
                    "type": "context",
                    "elements": [{
                        "type": "mrkdwn",
                        "text": f"Incident `{incident_id[:8]}` · {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} · QuerySentinel",
                    }],
                },
            ],
        }],
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(_WEBHOOK_URL, json=payload)
            r.raise_for_status()
            return {"status": "posted", "code": r.status_code}
    except Exception as e:
        logger.warning("Slack post failed (non-fatal): %s", e)
        return None


def post_incident_sync(incident_report: dict) -> Optional[dict]:
    """Sync version for threaded callers (Change Stream)."""
    if not is_configured():
        return None
    try:
        import asyncio
        return asyncio.run(post_incident(incident_report))
    except Exception as e:
        logger.warning("Slack sync post failed: %s", e)
        return None
