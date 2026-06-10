"""
GeminiKeyRotator — multiply effective RPM by rotating across multiple API keys.

Usage:
  Single key (existing):  GOOGLE_API_KEY=AIza...
  Multiple keys (new):    GOOGLE_API_KEYS=AIza...,AIza...,AIza...,AIza...

Strategy: always pick the key with the MOST remaining capacity (fewest calls
in the last 60 seconds). If all keys are at the per-key RPM ceiling, waits
until the earliest slot opens up.

Effective RPM = num_keys × rpm_per_key
  1 key  × 12 RPM = 12 RPM  → pipeline ~25s
  2 keys × 12 RPM = 24 RPM  → pipeline ~12s
  4 keys × 12 RPM = 48 RPM  → pipeline ~6s
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque

logger = logging.getLogger("qs_key_rotator")


class GeminiKeyRotator:
    """
    Rotating pool of Gemini API keys with per-key sliding-window rate tracking.

    Thread-safe via asyncio.Lock — safe for concurrent coroutines (ParallelAgent).
    Sets os.environ["GOOGLE_API_KEY"] to the chosen key before each call so ADK
    picks it up at request time.
    """

    def __init__(self, rpm_per_key: int = 12) -> None:
        self.rpm_per_key = rpm_per_key
        self._keys: list[str] = self._load_keys()
        # Per-key sliding window of call timestamps
        self._windows: dict[str, deque[float]] = {k: deque() for k in self._keys}
        self._lock: asyncio.Lock | None = None  # created lazily in async context
        logger.info(
            "KeyRotator: %d key(s) × %d RPM = %d effective RPM",
            len(self._keys), rpm_per_key, len(self._keys) * rpm_per_key,
        )

    # ── Key loading ───────────────────────────────────────────────────────────

    @staticmethod
    def _load_keys() -> list[str]:
        """
        Load keys from env in priority order:
          1. GOOGLE_API_KEYS (comma-separated, plural) — multi-key rotation
          2. GOOGLE_API_KEY  (singular)                — single key fallback
        """
        multi = os.environ.get("GOOGLE_API_KEYS", "").strip()
        if multi:
            keys = [k.strip() for k in multi.split(",") if k.strip()]
            if keys:
                return keys
        single = os.environ.get("GOOGLE_API_KEY", "").strip()
        return [single] if single else []

    def reload_keys(self) -> None:
        """Hot-reload keys from env (call if you update .env at runtime)."""
        new_keys = self._load_keys()
        for k in new_keys:
            if k not in self._windows:
                self._windows[k] = deque()
        self._keys = new_keys
        logger.info("KeyRotator reloaded: %d key(s)", len(self._keys))

    # ── Core API ──────────────────────────────────────────────────────────────

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def acquire(self) -> str:
        """
        Acquire a key slot. Returns the chosen key AND sets os.environ["GOOGLE_API_KEY"].

        If all keys are at capacity, sleeps until the earliest slot opens.
        Always called under the internal lock — no concurrent key mutations.
        """
        if not self._keys:
            raise RuntimeError(
                "No Gemini API keys found. "
                "Set GOOGLE_API_KEYS=key1,key2,... or GOOGLE_API_KEY=key in .env"
            )

        lock = self._get_lock()
        async with lock:
            while True:
                now = time.monotonic()

                # Evict timestamps older than 60s for all keys
                for k in self._keys:
                    while self._windows[k] and now - self._windows[k][0] > 60.0:
                        self._windows[k].popleft()

                # Pick the key with the most remaining capacity
                best_key = min(self._keys, key=lambda k: len(self._windows[k]))
                used = len(self._windows[best_key])

                if used < self.rpm_per_key:
                    self._windows[best_key].append(time.monotonic())
                    os.environ["GOOGLE_API_KEY"] = best_key
                    logger.debug(
                        "KeyRotator: key[...%s] %d/%d RPM used",
                        best_key[-6:], used + 1, self.rpm_per_key,
                    )
                    return best_key

                # All keys exhausted — find when the earliest call expires
                earliest = min(
                    self._windows[k][0]
                    for k in self._keys
                    if self._windows[k]
                )
                wait_s = max(61.0 - (now - earliest), 0.5)
                logger.info(
                    "KeyRotator: all %d key(s) at %d RPM — waiting %.1fs",
                    len(self._keys), self.rpm_per_key, wait_s,
                )
                await asyncio.sleep(wait_s)

    def status(self) -> dict:
        """Return current usage per key (for logging/debugging)."""
        now = time.monotonic()
        result = {}
        for k in self._keys:
            recent = sum(1 for t in self._windows[k] if now - t <= 60.0)
            result[f"...{k[-6:]}"] = {
                "calls_last_60s": recent,
                "capacity":       self.rpm_per_key,
                "available":      max(self.rpm_per_key - recent, 0),
            }
        return result


# ── Process-wide singleton ────────────────────────────────────────────────────
# Shared across all agents in all pipeline runs.
# rpm_per_key=12 → 3 under AI Studio's 15 RPM ceiling (headroom for bursts).

rotator = GeminiKeyRotator(rpm_per_key=12)
