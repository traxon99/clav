"""Story 4.3 — Alerter: severity-gated dispatch to pluggable AlertChannels.
CRITICAL sends immediately (deduped per condition); WARNING batches into a
periodic digest. A missing/failing channel never raises into the caller."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from clav.clock import FakeClock
from clav.domain.models import Alert, AlertSeverity
from clav.interfaces.alerting import AlertChannel
from clav.services.alerting import Alerter

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


class SpyChannel(AlertChannel):
    def __init__(self, *, raises: bool = False) -> None:
        self.received: list[Alert] = []
        self._raises = raises

    def send(self, alert: Alert) -> None:
        if self._raises:
            raise RuntimeError("channel boom")
        self.received.append(alert)


def _alerter(
    *,
    clock: FakeClock | None = None,
    channels: list[AlertChannel] | None = None,
    critical_dedup_minutes: int = 15,
    digest_interval_minutes: int = 60,
) -> tuple[Alerter, FakeClock]:
    clock = clock or FakeClock(NOW)
    return (
        Alerter(
            clock=clock,
            channels=channels if channels is not None else [],
            critical_dedup_minutes=critical_dedup_minutes,
            digest_interval_minutes=digest_interval_minutes,
        ),
        clock,
    )


def test_critical_sends_immediately() -> None:
    channel = SpyChannel()
    alerter, _ = _alerter(channels=[channel])

    alerter.notify("broker_down", "critical", "Alpaca unreachable")

    assert len(channel.received) == 1
    assert channel.received[0].condition == "broker_down"
    assert channel.received[0].severity == "critical"


def test_critical_deduped_within_window() -> None:
    channel = SpyChannel()
    alerter, clock = _alerter(channels=[channel], critical_dedup_minutes=15)

    alerter.notify("broker_down", "critical", "first")
    clock.advance(timedelta(minutes=5))
    alerter.notify("broker_down", "critical", "second")  # within window -> deduped

    assert len(channel.received) == 1
    assert channel.received[0].message == "first"


def test_critical_resends_after_dedup_window_elapses() -> None:
    channel = SpyChannel()
    alerter, clock = _alerter(channels=[channel], critical_dedup_minutes=15)

    alerter.notify("broker_down", "critical", "first")
    clock.advance(timedelta(minutes=16))
    alerter.notify("broker_down", "critical", "second")

    assert len(channel.received) == 2


def test_different_conditions_are_not_deduped_against_each_other() -> None:
    channel = SpyChannel()
    alerter, _ = _alerter(channels=[channel])

    alerter.notify("broker_down", "critical", "a")
    alerter.notify("disk_pressure", "critical", "b")

    assert {a.condition for a in channel.received} == {"broker_down", "disk_pressure"}


def test_warning_does_not_send_immediately() -> None:
    channel = SpyChannel()
    alerter, _ = _alerter(channels=[channel])

    alerter.notify("memory_pressure", "warning", "free memory low")

    assert channel.received == []


def test_warning_batches_into_a_digest_after_the_interval() -> None:
    channel = SpyChannel()
    alerter, clock = _alerter(channels=[channel], digest_interval_minutes=60)

    alerter.notify("memory_pressure", "warning", "free memory low")
    alerter.notify("llm_budget_exhausted", "warning", "budget spent")
    assert channel.received == []  # still buffered

    clock.advance(timedelta(minutes=61))
    alerter.tick()

    assert len(channel.received) == 1
    digest = channel.received[0]
    assert digest.condition == "digest"
    assert digest.severity == "warning"
    assert "memory_pressure" in digest.context["conditions"]
    assert "llm_budget_exhausted" in digest.context["conditions"]


def test_tick_is_a_noop_before_the_interval_elapses() -> None:
    channel = SpyChannel()
    alerter, clock = _alerter(channels=[channel], digest_interval_minutes=60)

    alerter.notify("memory_pressure", "warning", "free memory low")
    clock.advance(timedelta(minutes=30))
    alerter.tick()

    assert channel.received == []


def test_tick_with_no_pending_warnings_is_a_noop() -> None:
    channel = SpyChannel()
    alerter, clock = _alerter(channels=[channel], digest_interval_minutes=60)

    clock.advance(timedelta(minutes=120))
    alerter.tick()  # nothing buffered -- nothing to send

    assert channel.received == []


def test_digest_clears_after_flushing() -> None:
    channel = SpyChannel()
    alerter, clock = _alerter(channels=[channel], digest_interval_minutes=60)

    alerter.notify("memory_pressure", "warning", "first batch")
    clock.advance(timedelta(minutes=61))
    alerter.tick()
    assert len(channel.received) == 1

    # A second warning right after the flush shouldn't immediately re-flush.
    alerter.notify("disk_pressure", "warning", "second batch")
    assert len(channel.received) == 1


def test_no_channels_configured_never_raises() -> None:
    alerter, _ = _alerter(channels=[])
    alerter.notify("broker_down", "critical", "no channels configured")
    alerter.notify("memory_pressure", "warning", "still fine")
    alerter.tick()  # nothing to assert -- just must not raise


def test_a_raising_channel_is_caught_and_does_not_block_others() -> None:
    bad = SpyChannel(raises=True)
    good = SpyChannel()
    alerter, _ = _alerter(channels=[bad, good])

    alerter.notify("broker_down", "critical", "boom expected")

    assert good.received  # the good channel still got it
    assert good.received[0].condition == "broker_down"


# --- Story 4.10: severity routing is a property, not per-condition luck -----


@pytest.mark.parametrize("severity", ["critical", "warning"])
def test_every_condition_dispatches_with_exactly_the_severity_it_was_raised_at(
    severity: AlertSeverity,
) -> None:
    """Whatever condition name/message is passed, the Alert that eventually
    reaches a channel carries that exact severity -- critical never
    downgrades to a digest, warning never sends immediately."""
    channel = SpyChannel()
    alerter, clock = _alerter(channels=[channel], digest_interval_minutes=60)

    alerter.notify("some_condition", severity, "some message")
    if severity == "warning":
        assert channel.received == []  # buffered, not sent yet
        clock.advance(timedelta(minutes=61))
        alerter.tick()

    assert len(channel.received) == 1
    assert channel.received[0].severity == severity
