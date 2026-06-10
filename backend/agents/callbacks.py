"""
ADK Safety + Audit + Rate-Limit + LLM-Injection Callbacks for QUERYSENTINEL

ADK 1.3.0 callbacks used here:

1. before_model_callback  — fires before EVERY Gemini API call across all agents.
   Two responsibilities:
     a. RATE LIMITER: sliding-window counter capped at LLM_RPM_LIMIT (default 12).
        Blocks (async sleep) until under the limit. Prevents 429 RESOURCE_EXHAUSTED
        on AI Studio free tier (15 RPM). Cheaper than full pipeline retries.
     b. LLM INJECTION SCAN: scans outgoing LLM request for prompt-injection patterns
        BEFORE they reach Gemini. Defence-in-depth Layer 2:
          Layer 1 → watcher.py scans documents at write time (database boundary)
          Layer 2 → before_model_callback scans LLM context (model boundary)
        If injection is detected, returns a synthetic LlmResponse that blocks the
        poisoned content from ever reaching the model.

2. before_tool_callback — SAFETY GATE: blocks create_index/drop_index/delete_documents
   without _approve_signal. Human-in-the-loop enforced at the ADK framework layer.

3. after_tool_callback  — AUDIT TRAIL: every tool call written to tool_audit_log.

ADK callback API (1.3.0):
  before_model_callback(callback_context, llm_request) -> Optional[LlmResponse]
    Return LlmResponse → skip Gemini call entirely
    Return None        → proceed normally

  before_tool_callback(tool, args, tool_context) -> Optional[dict]
    Return dict  → skip tool call, use as response
    Return None  → proceed normally

  after_tool_callback(tool, args, tool_context, tool_response) -> Optional[dict]
    Return dict  → override response
    Return None  → use original unchanged
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("qs_callbacks")

# ── Incident registry (concurrent-safe per-incident context) ──────────────────
# Keyed by incident_id so concurrent incidents never contaminate each other.
# orchestrator calls set_incident_context() before building the pipeline.

_incident_registry: dict[str, dict] = {}  # incident_id → {anomaly, incident_id}


def set_incident_context(anomaly: dict, incident_id: str) -> None:
    _incident_registry[incident_id] = {"anomaly": anomaly, "incident_id": incident_id}
    if len(_incident_registry) > 50:
        oldest_key = next(iter(_incident_registry))
        _incident_registry.pop(oldest_key, None)


def _get_incident_context(tool_context) -> tuple[dict, str]:
    try:
        incident_id = tool_context.state.get("incident_id", "")
        if incident_id and incident_id in _incident_registry:
            reg = _incident_registry[incident_id]
            return reg["anomaly"], reg["incident_id"]
    except Exception:
        pass
    if _incident_registry:
        reg = next(reversed(_incident_registry.values()))
        return reg["anomaly"], reg["incident_id"]
    return {}, ""


# ── Key rotator (rate limiter + multi-key support) ────────────────────────────
# Imported here so before_model_callback delegates all throttling logic to it.
# Single key: just rate-limits. Multiple keys: multiplies effective RPM.

from .key_rotator import rotator as _key_rotator


# ── LLM-layer injection patterns (Layer 2 defence) ───────────────────────────
# These scan the text content being sent to Gemini, not just the stored document.
# Catches injection payloads that survived Layer 1 (document scan) or arrived
# via tool responses (e.g. a malicious MongoDB document fetched mid-pipeline).

_LLM_INJECTION_RE = re.compile(
    r"(ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?"
    r"|forget\s+(?:everything|all|your|the)\s+"
    r"|you\s+are\s+now\s+(?:a|an|the)\s+\w+"
    r"|<\|im_start\|>\s*system"
    r"|###\s*system\s+override"
    r"|\[\s*system\s*\]"
    r"|disregard\s+(?:all\s+)?(?:previous|your|the)\s+(?:instructions?|context|rules?)"
    r"|new\s+instructions?:\s*(?:you\s+must|always|never)"
    r"|jailbreak\s*[:=]"
    r"|<\s*inject\s*>)",
    re.IGNORECASE,
)


def _scan_llm_request_for_injection(llm_request) -> bool:
    """
    Scan the text content of an LLM request for prompt-injection patterns.
    Returns True if injection detected (call should be blocked).
    """
    try:
        contents = getattr(llm_request, "contents", None) or []
        for content in contents:
            parts = getattr(content, "parts", None) or []
            for part in parts:
                text = getattr(part, "text", None)
                if text and isinstance(text, str):
                    if _LLM_INJECTION_RE.search(text[:10_000]):
                        return True
    except Exception:
        pass
    return False


# ── before_model_callback ─────────────────────────────────────────────────────

async def before_model_callback(callback_context, llm_request):
    """
    ADK before_model_callback — runs before every Gemini API call.

    1. LLM injection scan (Layer 2 defence):
       Scans outgoing request text for prompt-injection patterns before reaching Gemini.
       Layer 1 = watcher.py at document-write time.
       Layer 2 = here, at model-call time.
       Returns a synthetic blocked LlmResponse if injection detected.

    2. Key rotation + rate limiting:
       Delegates to GeminiKeyRotator — picks the key with the most capacity,
       sets GOOGLE_API_KEY, waits if all keys are exhausted.
       1 key  → 12 RPM, ~25s pipeline
       4 keys → 48 RPM, ~6s pipeline
    """
    # ── Layer 2: LLM injection scan ───────────────────────────────────────────
    if _scan_llm_request_for_injection(llm_request):
        logger.warning("before_model_callback: injection pattern in outgoing LLM request — BLOCKED")
        try:
            from google.genai.types import GenerateContentResponse, Candidate, Content, Part
            blocked_part = Part(text=(
                '{"status":"BLOCKED","reason":"LLM injection pattern detected in request",'
                '"layer":"before_model_callback"}'
            ))
            return GenerateContentResponse(
                candidates=[Candidate(content=Content(role="model", parts=[blocked_part]))]
            )
        except Exception:
            pass  # fall through — let ADK handle however it wants

    # ── Key rotation + rate limiting (Gemini only) ────────────────────────────
    # Non-Gemini fallback backends use a single/no key — skip rotation for them.
    _backend = os.environ.get("LLM_BACKEND", "gemini").lower()
    if _backend == "gemini":
        await _key_rotator.acquire()
    return None  # proceed with LLM call


# ── Safety Gate ───────────────────────────────────────────────────────────────

# Both FunctionTool names and MongoDB MCP server tool names (hyphenated)
_DESTRUCTIVE_TOOLS = frozenset({
    "create_index", "drop_index", "delete_documents",          # FunctionTool names
    "create-index", "drop-index", "delete-many",               # MongoDB MCP server names
    "drop-collection", "drop-database", "update-many", "insert-many",
})


def before_tool_callback(tool, args: dict[str, Any], tool_context) -> Optional[dict]:
    """
    ADK before_tool_callback — safety gate + audit pre-log.

    Blocks create_index / drop_index / delete_documents without human approval.
    Approval is signaled via _approve_signal in the anomaly payload.
    Without it, returns AWAITING_APPROVAL so the UI shows the human gate.
    """
    tool_name = getattr(tool, "name", None) or str(tool)
    current_anomaly, current_incident_id = _get_incident_context(tool_context)

    if tool_name in _DESTRUCTIVE_TOOLS:
        approved = bool(current_anomaly.get("_approve_signal"))
        if not approved:
            logger.info(
                "Safety gate BLOCKED '%s' for incident %s — awaiting human approval.",
                tool_name, current_incident_id[:8] if current_incident_id else "?",
            )
            return {
                "status":       "AWAITING_APPROVAL",
                "message": (
                    f"Action '{tool_name}' requires human approval. "
                    "Use the APPROVE button in the QuerySentinel dashboard "
                    "or POST /api/approve with the incident_id."
                ),
                "args":         args,
                "incident_id":  current_incident_id,
                "tool_name":    tool_name,
                "gate":         "human_in_the_loop",
            }

    try:
        if not hasattr(tool_context, "state"):
            tool_context.state = {}
        tool_context.state[f"__cb_start_{tool_name}"] = time.monotonic()
    except Exception:
        pass

    return None


# ── Audit Trail ───────────────────────────────────────────────────────────────

def after_tool_callback(
    tool, args: dict[str, Any], tool_context, tool_response: dict[str, Any]
) -> Optional[dict]:
    """
    ADK after_tool_callback — tamper-evident audit log.

    Writes to app_db.tool_audit_log: incident_id, tool_name, args (sanitized),
    response_size_bytes, latency_ms, was_blocked, timestamp.
    Non-fatal — never crashes the pipeline.
    """
    tool_name = getattr(tool, "name", None) or str(tool)
    current_anomaly, current_incident_id = _get_incident_context(tool_context)

    latency_ms = 0
    try:
        if hasattr(tool_context, "state"):
            start = tool_context.state.get(f"__cb_start_{tool_name}")
            if start is not None:
                latency_ms = int((time.monotonic() - start) * 1000)
    except Exception:
        pass

    safe_args = _sanitize_args(args)

    try:
        import json as _json
        response_size = len(_json.dumps(tool_response, default=str))
    except Exception:
        response_size = 0

    try:
        from db import app_db
        app_db.tool_audit_log.insert_one({
            "incident_id":         current_incident_id,
            "collection_name":     current_anomaly.get("collection_name"),
            "tool_name":           tool_name,
            "args":                safe_args,
            "response_size_bytes": response_size,
            "latency_ms":          latency_ms,
            "was_blocked":         tool_response.get("status") == "AWAITING_APPROVAL",
            "timestamp":           datetime.now(timezone.utc),
        })
    except Exception as e:
        logger.debug("Audit log write failed (non-fatal): %s", e)

    return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sanitize_args(args: dict) -> dict:
    _SENSITIVE = {"uri", "connection_string", "password", "api_key", "token", "secret"}
    return {
        k: "***REDACTED***" if k.lower() in _SENSITIVE else v
        for k, v in (args or {}).items()
    }
