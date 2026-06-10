"""
AI Memory Poisoning Detector — OWASP LLM01 + LLM03

MongoDB is increasingly used as the memory layer for AI agents.
MongoDB keynote 2025: "the database is the memory that allows agents to perceive context."

This makes MongoDB a new attack surface that NO existing monitoring tool covers:
  - Prompt injection patterns planted in stored documents
  - Adversarial content designed to hijack downstream LLM behavior
  - Training data poisoning via legitimate-looking document ingestion
  - System prompt override attempts via retrieved context

OWASP LLM Top 10 coverage:
  LLM01 — Prompt Injection:        detect injected instructions in document text fields
  LLM03 — Training Data Poisoning: detect semantic contamination (see semantic_velocity.py)
  LLM04 — Model DoS:               detect document floods targeting embedding collections
  LLM05 — Supply Chain:            detect schema mutations that corrupt agent memory structure

Usage:
    from detectors.prompt_injection import scan_document_for_ai_threats

    result = scan_document_for_ai_threats(document, collection_name="incidents")
    if result["threat_detected"]:
        # anomaly_type = "AI_MEMORY_POISONING"
        # owasp_id     = "LLM01"
        ...
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("prompt_injection_detector")


# ── OWASP LLM Top 10 mapping for all QuerySentinel anomaly types ─────────────
# Every incident report now carries an owasp_classification field.
# This makes QuerySentinel speak the language of security teams and CISOs.
OWASP_LLM_MAP: dict[str, dict[str, str]] = {
    "DOC_SIZE_SPIKE": {
        "id":   "LLM04",
        "name": "Model Denial of Service",
        "desc": "Abnormally large documents may be designed to exhaust LLM context windows or embedding resources.",
    },
    "SCHEMA_DRIFT": {
        "id":   "LLM05",
        "name": "Supply Chain Vulnerabilities",
        "desc": "Unauthorized schema changes can corrupt the structure of agent memory, causing downstream tool-call failures.",
    },
    "SEMANTIC_VELOCITY_SPIKE": {
        "id":   "LLM03",
        "name": "Training Data Poisoning",
        "desc": "Semantic centroid shifted beyond 3-sigma baseline. Content meaning has fundamentally changed — consistent with adversarial ingestion.",
    },
    "AI_MEMORY_POISONING": {
        "id":   "LLM01",
        "name": "Prompt Injection",
        "desc": "Document content contains patterns designed to override LLM system prompts or manipulate agent behavior when retrieved as context.",
    },
    "ETL_MISCONFIGURATION": {
        "id":   "LLM03",
        "name": "Training Data Poisoning",
        "desc": "Misconfigured ingestion pipeline polluting agent memory with semantically alien content.",
    },
    "LLM_FLOOD": {
        "id":   "LLM04",
        "name": "Model Denial of Service",
        "desc": "High-volume document flood targeting collections used as LLM retrieval sources.",
    },
    "DATA_POISONING": {
        "id":   "LLM03",
        "name": "Training Data Poisoning",
        "desc": "Deliberately malformed documents intended to bias RAG retrieval results.",
    },
    "CLUSTER_WIDE_EVENT": {
        "id":   "LLM05",
        "name": "Supply Chain Vulnerabilities",
        "desc": "Coordinated anomaly across multiple collections — consistent with a supply-chain compromise affecting multiple agent memory stores simultaneously.",
    },
    "UNKNOWN": {
        "id":   "LLM05",
        "name": "Supply Chain Vulnerabilities",
        "desc": "Unclassified anomaly — review manually for supply chain indicators.",
    },
}


# ── Prompt injection signature patterns ──────────────────────────────────────
# Curated from OWASP LLM01 research, real-world red-team findings, and
# documented adversarial attacks against RAG systems (2024–2025).
_RAW_PATTERNS: list[tuple[str, str, str]] = [
    # Direct instruction override
    (r"(?i)ignore\s+(previous|above|prior|all)\s+(instructions?|prompt|context|directives?)",
     "INSTRUCTION_OVERRIDE",
     "Classic prompt injection: attempts to nullify prior system instructions."),

    (r"(?i)forget\s+(everything|all)\s+(you|I|we)\s+(know|said|told|learned|were told)",
     "CONTEXT_WIPE",
     "Memory wipe attack: attempts to erase prior context from LLM working memory."),

    (r"(?i)disregard\s+(your|all|previous|any)\s+(training|instructions?|guidelines?|rules?|constraints?)",
     "TRAINING_BYPASS",
     "Training bypass: attempts to override model alignment constraints."),

    # System prompt injection
    (r"(?i)\[system\s*\]|\[sys\]|<\|system\|>|<system>|<\|im_start\|>\s*system",
     "SYSTEM_PROMPT_INJECTION",
     "Chat template injection: embeds fake system-role tokens to override prompt structure."),

    (r"(?i)###\s*(system|instruction|context)\s*:|SYSTEM\s*PROMPT\s*:|NEW\s*INSTRUCTIONS?\s*:",
     "SYSTEM_PROMPT_INJECTION",
     "Header-style system prompt injection — common in RAG document attacks."),

    (r"(?i)###\s*system\s+(override|injection|bypass|prompt|manipulation)",
     "SYSTEM_PROMPT_INJECTION",
     "Block-marker system override — attempts to signal an authority-level instruction change."),

    # Persona and identity hijacking
    (r"(?i)act\s+as\s+(if\s+you\s+are|a|an)\s+(different|evil|unrestricted|jailbroken|unfiltered)",
     "PERSONA_INJECTION",
     "Persona injection: attempts to force a different operational identity on the LLM."),

    (r"(?i)you\s+(are\s+)?(now|henceforth)\s+(free|unrestricted|liberated|DAN|GPT|Claude|Gemini)",
     "IDENTITY_OVERRIDE",
     "Identity override: attempts to rebind the LLM's self-model to a malicious persona."),

    # DAN-style jailbreaks
    (r"(?i)do\s+anything\s+now|DAN\s+mode|developer\s+mode\s+enabled|jailbreak(\s+mode)?",
     "JAILBREAK_ATTEMPT",
     "Known jailbreak trigger phrase — DAN or developer mode variants."),

    # Prompt exfiltration
    (r"(?i)(print|repeat|output|return|show|reveal|display)\s+(back\s+)?(verbatim|exactly|all\s+of)?\s*(the\s+)?"
     r"(system|initial|hidden|original|first)\s+(prompt|instructions?|context|message)",
     "PROMPT_EXFILTRATION",
     "Attempts to exfiltrate system prompt content via retrieval — OWASP LLM06 overlap."),

    # Instruction planting (deferred injection)
    (r"(?i)when\s+(you|the\s+(AI|assistant|model|agent))\s+(is\s+asked|receives|processes|answers)",
     "DEFERRED_INJECTION",
     "Time-delayed injection: plants conditional instructions in stored memory for later activation."),

    (r"(?i)(next|from now on|whenever|every time)\s+(time\s+)?(you|the\s+(AI|agent))\s+(is\s+asked|respond)",
     "DEFERRED_INJECTION",
     "Persistent injection: attempts to permanently alter LLM behavior for future queries."),

    # Tool/function call injection
    (r"(?i)call\s+(the\s+)?(tool|function|api|endpoint)\s+[\"']?(create_index|drop_collection|delete)",
     "TOOL_INJECTION",
     "Tool call injection: attempts to trigger destructive tool actions via retrieved context."),

    (r"(?i)(execute|run|invoke)\s+(this\s+)?(code|command|query|script)\s*:",
     "CODE_INJECTION_ATTEMPT",
     "Code execution injection: attempts to run arbitrary code through LLM tool use."),
]

# Compile all patterns once at import time
_COMPILED_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(pat), label, desc)
    for pat, label, desc in _RAW_PATTERNS
]


# ── Suspicious field names (structural injection indicators) ─────────────────
_SUSPICIOUS_FIELDS: frozenset[str] = frozenset({
    "system_prompt", "prompt_override", "llm_instruction", "ai_context",
    "hidden_instruction", "__prompt__", "_system", "override_context",
    "inject", "jailbreak", "adversarial", "bypass", "override",
    "llm_context", "agent_instruction", "rag_injection",
})


# ── Public API ────────────────────────────────────────────────────────────────

def scan_document_for_ai_threats(
    doc: dict[str, Any],
    collection_name: str = "unknown",
) -> dict:
    """
    Scan a MongoDB document for AI memory poisoning patterns.

    Checks:
      1. All string field values for known prompt injection signatures
      2. Field names for suspicious injection-related keys
      3. Severity scoring: multiple matches → CRITICAL, single → WARNING

    Returns:
        {
            "threat_detected": bool,
            "anomaly_type":    "AI_MEMORY_POISONING" | None,
            "severity":        "CRITICAL" | "WARNING" | None,
            "matches":         list[{field, pattern_label, description, matched_text}],
            "suspicious_fields": list[str],
            "confidence":      float,
            "owasp":           {"id": "LLM01", "name": "Prompt Injection", ...},
            "scanned_at":      datetime,
        }
    """
    matches: list[dict] = []
    suspicious_fields: list[str] = []

    # Check field names
    for key in doc:
        if str(key).lower() in _SUSPICIOUS_FIELDS:
            suspicious_fields.append(key)

    # Check string values recursively
    _scan_values(doc, matches, path="")

    threat_detected = bool(matches or suspicious_fields)
    confidence = min(1.0, (len(matches) * 0.35 + len(suspicious_fields) * 0.2))
    severity = None

    if threat_detected:
        severity = "CRITICAL" if (len(matches) >= 2 or confidence >= 0.7) else "WARNING"
        logger.warning(
            "AI_MEMORY_POISONING in '%s': %d pattern match(es), %d suspicious field(s). "
            "Confidence=%.2f Severity=%s",
            collection_name, len(matches), len(suspicious_fields), confidence, severity,
        )

    return {
        "threat_detected":    threat_detected,
        "anomaly_type":       "AI_MEMORY_POISONING" if threat_detected else None,
        "severity":           severity,
        "matches":            matches,
        "suspicious_fields":  suspicious_fields,
        "confidence":         round(confidence, 3),
        "owasp":              OWASP_LLM_MAP["AI_MEMORY_POISONING"] if threat_detected else {},
        "scanned_at":         datetime.now(timezone.utc),
    }


def get_owasp_classification(anomaly_type: str) -> dict:
    """
    Return the OWASP LLM classification for a given QuerySentinel anomaly type.
    Used to enrich every incident_report with security-standard context.
    """
    return OWASP_LLM_MAP.get(anomaly_type, OWASP_LLM_MAP["UNKNOWN"])


# ── Internal helpers ─────────────────────────────────────────────────────────

def _scan_values(
    obj: Any,
    matches: list,
    path: str,
    depth: int = 0,
) -> None:
    """
    Recursively walk document fields and test every string value
    against the compiled injection pattern set.

    Depth-limited to 5 levels to avoid infinite recursion on pathological docs.
    """
    if depth > 5:
        return

    if isinstance(obj, str):
        _test_string(obj, path, matches)

    elif isinstance(obj, dict):
        for key, val in obj.items():
            child_path = f"{path}.{key}" if path else key
            _scan_values(val, matches, child_path, depth + 1)

    elif isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            _scan_values(item, matches, f"{path}[{i}]", depth + 1)


def _test_string(text: str, field_path: str, matches: list) -> None:
    """Test one string value against all compiled injection patterns."""
    if len(text) < 10:          # too short to be meaningful
        return
    if len(text) > 50_000:      # very long blobs — truncate for regex safety
        text = text[:50_000]

    seen_labels: set[str] = set()
    for compiled_re, label, description in _COMPILED_PATTERNS:
        if label in seen_labels:
            continue  # one match per label per field is enough
        m = compiled_re.search(text)
        if m:
            seen_labels.add(label)
            matches.append({
                "field":        field_path,
                "pattern_label": label,
                "description":  description,
                "matched_text": m.group(0)[:120],  # truncate for storage
            })
