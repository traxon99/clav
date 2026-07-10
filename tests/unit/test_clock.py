from datetime import UTC, datetime, timedelta

import pytest

from clav.clock import FakeClock, SystemClock


def test_system_clock_returns_timezone_aware_utc_now() -> None:
    before = datetime.now(UTC)
    now = SystemClock().now()
    after = datetime.now(UTC)

    assert now.tzinfo is not None
    assert before <= now <= after


def test_fake_clock_controls_now() -> None:
    fixed = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    clock = FakeClock(fixed)

    assert clock.now() == fixed
    assert clock.now() == fixed  # calling now() twice never advances it itself


def test_fake_clock_advance() -> None:
    clock = FakeClock(datetime(2025, 6, 1, 12, 0, tzinfo=UTC))
    clock.advance(timedelta(minutes=30))
    assert clock.now() == datetime(2025, 6, 1, 12, 30, tzinfo=UTC)


def test_fake_clock_set() -> None:
    clock = FakeClock()
    target = datetime(2030, 1, 1, tzinfo=UTC)
    clock.set(target)
    assert clock.now() == target


def test_fake_clock_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime(2025, 6, 1, 12, 0))

    clock = FakeClock()
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set(datetime(2025, 6, 1, 12, 0))
