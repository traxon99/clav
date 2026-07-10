"""Clock abstraction so decision logic never reads wall-clock time directly.

Every module that needs "now" takes an injected ``Clock`` instead of calling
``datetime.now()``/``time.time()`` itself. This is what makes the decision engine,
risk rules, and cooldown/trading-hours checks deterministically unit-testable via
``FakeClock`` and reproducible in backtests.

``tests/unit/test_architecture.py`` enforces this as an automated rule: no file
under ``src/clav`` other than this one may call the stdlib wall-clock primitives.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        """Current time, timezone-aware, in UTC."""


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock(Clock):
    """Deterministic clock for tests and backtests. Never advances on its own."""

    def __init__(self, initial: datetime | None = None) -> None:
        self._now = initial or datetime(2024, 1, 1, tzinfo=UTC)
        if self._now.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware datetime")

    def now(self) -> datetime:
        return self._now

    def set(self, dt: datetime) -> None:
        if dt.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware datetime")
        self._now = dt

    def advance(self, delta: timedelta) -> None:
        self._now += delta
