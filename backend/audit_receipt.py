"""
audit_receipt.py — Tamper-evident cryptographic receipts for QUERYSENTINEL.

QuerySentinel detects tampering in YOUR MongoDB data. This module makes
QuerySentinel's OWN forensic output tamper-evident too: every incident report
and every human APPROVE decision is sealed with a 128-byte Ed25519 receipt
(via AetherProof). Anyone can later verify a MongoDB document was not altered
after QuerySentinel signed it — offline, with the public key.

This is "MongoDB data integrity," not generic crypto: the receipts live inside
the incident_reports documents in MongoDB, and verification proves the stored
document is byte-identical to what the agents actually found.

API:
  sign_payload(doc)            -> receipt_hex  (sign a forensic payload)
  verify_payload(doc, hex)     -> {verified, tampered, ...}  (check integrity)

Key custody: QuerySentinel holds the signing key, derived from AETHERPROOF_SEED
(env). If unset, a deterministic project seed is used so the demo is reproducible.
Production roadmap = TPM/KMS-bound keys (not claimed here).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger("audit_receipt")

# Keys that must NOT be part of the signed payload (volatile or the receipt itself)
_EXCLUDE_FROM_SIGNATURE = frozenset({
    "aetherproof_receipt", "receipt_verified", "receipt_pretty",
    "created_at", "_id",
})

_signing_key = None
_verifying_key = None
_AVAILABLE = False

try:
    import aetherproof as _ap
    _AVAILABLE = True
except Exception as e:  # pragma: no cover
    logger.warning("aetherproof not available (%s) — receipts disabled", e)


def _get_keys():
    """Lazily build a server-held signing/verifying keypair from a seed."""
    global _signing_key, _verifying_key
    if not _AVAILABLE:
        return None, None
    if _signing_key is not None:
        return _signing_key, _verifying_key
    try:
        seed_str = os.getenv("AETHERPROOF_SEED", "querysentinel-mongodb-track-2026")
        seed = hashlib.sha256(seed_str.encode()).digest()  # 32 bytes
        # AetherProof SigningKey from a 32-byte seed; fall back to dev key.
        try:
            _signing_key = _ap.SigningKey.from_seed(seed)
        except Exception:
            _signing_key = _ap.dev_signing_key()
        try:
            _verifying_key = _signing_key.verifying_key()
        except Exception:
            _verifying_key = _ap.dev_verifying_key()
    except Exception as e:
        logger.warning("Could not build signing keys (%s)", e)
        _signing_key = _verifying_key = None
    return _signing_key, _verifying_key


def _canonical_bytes(doc: dict) -> bytes:
    """Deterministic byte representation of the forensic payload (sorted, stable)."""
    payload = {k: v for k, v in doc.items() if k not in _EXCLUDE_FROM_SIGNATURE}
    return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()


def sign_payload(doc: dict) -> Optional[str]:
    """
    Generate a tamper-evident receipt for a forensic document.
    Returns the 128-byte receipt as hex, or None if unavailable.
    """
    if not _AVAILABLE:
        return None
    sk, _ = _get_keys()
    if sk is None:
        return None
    try:
        data = _canonical_bytes(doc)
        receipt = _ap.generate(data, signing_key=sk)
        return receipt.to_bytes().hex()
    except Exception as e:
        logger.warning("sign_payload failed: %s", e)
        return None


def verify_payload(doc: dict, receipt_hex: Optional[str] = None) -> dict:
    """
    Verify a document against its receipt.

    Returns:
      {
        "available":  bool,   # is the receipt subsystem usable
        "has_receipt": bool,
        "verified":   bool,   # signature authentic AND content unchanged
        "tampered":   bool,   # signature authentic but content was altered
        "reason":     str,
      }
    """
    out = {"available": _AVAILABLE, "has_receipt": False,
           "verified": False, "tampered": False, "reason": ""}
    if not _AVAILABLE:
        out["reason"] = "aetherproof not installed"
        return out

    receipt_hex = receipt_hex or doc.get("aetherproof_receipt")
    if not receipt_hex:
        out["reason"] = "no receipt on document"
        return out
    out["has_receipt"] = True

    try:
        _, vk = _get_keys()
        receipt = _ap.Receipt.from_bytes(bytes.fromhex(receipt_hex))
        sig_ok = bool(receipt.verify(vk))                    # signature authentic?
        current_hash = _ap.fnv1a(_canonical_bytes(doc))      # content hash now
        content_ok = (receipt.binary_hash == current_hash)   # unchanged?

        if sig_ok and content_ok:
            out["verified"] = True
            out["reason"] = "signature valid, content unchanged"
        elif sig_ok and not content_ok:
            out["tampered"] = True
            out["reason"] = "VALID SIGNATURE but content was ALTERED after signing"
        else:
            out["reason"] = "signature invalid"
    except Exception as e:
        out["reason"] = f"verify error: {e}"
    return out


def receipt_pretty(receipt_hex: str) -> str:
    """Human-readable receipt dump for the UI / demo."""
    if not _AVAILABLE or not receipt_hex:
        return ""
    try:
        return _ap.Receipt.from_bytes(bytes.fromhex(receipt_hex)).pretty()
    except Exception:
        return ""


def is_available() -> bool:
    return _AVAILABLE
