"""
QUERYSENTINEL — Threat Detectors Package

MongoDB is the memory layer for AI agents (MongoDB keynote 2025).
That makes it an OWASP LLM Top 10 attack surface.

Detectors in this package operate at the database layer — before malicious content
reaches your LLM pipeline.

  prompt_injection.py — OWASP LLM01: Prompt Injection + LLM03: Training Data Poisoning
"""
from .prompt_injection import scan_document_for_ai_threats, OWASP_LLM_MAP, get_owasp_classification

__all__ = ["scan_document_for_ai_threats", "OWASP_LLM_MAP", "get_owasp_classification"]
