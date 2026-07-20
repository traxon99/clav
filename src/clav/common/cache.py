"""In-memory TTL fetch guard (Story 3.3).

Prevents re-fetching the same ``(source, symbol)`` within ``ttl_seconds`` — the
RAM-side companion to the DB-side content-hash dedup. Time comes from an injected
``Clock`` (never wall-clock), so it's deterministic under ``FakeClock``.
"""

from __future__ import annotations

from datetime import datetime

from clav.clock import Clock


class TtlCache:
    def __init__(self, *, clock: Clock, ttl_seconds: float) -> None:
        self._clock = clock
        self._ttl = ttl_seconds
        self._last: dict[str, datetime] = {}

    def should_fetch(self, key: str) -> bool:
        """True if ``key`` was never fetched or its TTL has elapsed."""
        last = self._last.get(key)
        if last is None:
            return True
        return (self._clock.now() - last).total_seconds() >= self._ttl

    def mark(self, key: str) -> None:
        self._last[key] = self._clock.now()

    def clear(self) -> None:
        self._last.clear()
