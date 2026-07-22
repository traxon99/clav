"""Alerter — severity-gated dispatch to pluggable ``AlertChannel``s (Story 4.3).

``CRITICAL`` alerts send immediately, deduped per ``condition`` within a
configurable window so a persisting fault doesn't repage every cycle.
``WARNING`` alerts batch into a periodic digest instead of paging one at a
time. Deliberately **DB-free**: persisting the corresponding
``category="alert"`` ``health_event`` row is the caller's job, using
whatever session it already has open (``HealthMonitor``'s cycle-end
transaction, or ``ExecutionEngine``/``ScanCycleService``'s) — opening a
second writing session from here could contend for SQLite's single writer
lock with that in-flight transaction. A channel that raises is always
caught and logged; a missing/unconfigured channel list is simply a no-op.

``live_mode`` (Story 6.5, off by default): when true, every ``notify()``
call is escalated to ``critical`` regardless of the severity it was raised
at — with real money at stake, nothing sits in the warning digest waiting
for the next flush.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from clav.clock import Clock
from clav.common.logging import get_logger
from clav.domain.models import Alert, AlertSeverity
from clav.interfaces.alerting import AlertChannel

_logger = get_logger(__name__)


class Alerter:
    def __init__(
        self,
        *,
        clock: Clock,
        channels: list[AlertChannel],
        critical_dedup_minutes: int,
        digest_interval_minutes: int,
        live_mode: bool = False,
    ) -> None:
        self._clock = clock
        self._channels = channels
        self._dedup_window = timedelta(minutes=critical_dedup_minutes)
        self._digest_interval = timedelta(minutes=digest_interval_minutes)
        self._live_mode = live_mode
        self._last_sent: dict[str, datetime] = {}
        self._pending_warnings: list[Alert] = []
        self._last_digest_flush: datetime | None = None

    def notify(
        self,
        condition: str,
        severity: AlertSeverity,
        message: str,
        context: dict[str, object] | None = None,
    ) -> None:
        # Story 6.5: with real money at stake, nothing waits for a digest —
        # every alert that would otherwise batch as a warning escalates to
        # critical and pages immediately.
        if self._live_mode and severity != "critical":
            severity = "critical"

        alert = Alert(
            condition=condition, severity=severity, message=message, context=context or {}
        )
        log = _logger.critical if severity == "critical" else _logger.warning
        log("alert_raised", condition=condition, severity=severity, message=message)

        if severity == "critical":
            self._send_critical(alert)
        else:
            self._pending_warnings.append(alert)
            self.tick()

    def tick(self) -> None:
        """Flush the pending warning digest if the interval has elapsed.
        Call once per cycle regardless of whether ``notify`` was — a quiet
        period after some buffered warnings still delivers on schedule."""
        now = self._clock.now()
        if self._last_digest_flush is None:
            self._last_digest_flush = now
        if not self._pending_warnings:
            return
        if now - self._last_digest_flush < self._digest_interval:
            return
        self._flush_digest()

    def _flush_digest(self) -> None:
        batch, self._pending_warnings = self._pending_warnings, []
        self._last_digest_flush = self._clock.now()
        summary = "\n".join(f"- [{a.condition}] {a.message}" for a in batch)
        digest = Alert(
            condition="digest",
            severity="warning",
            message=f"{len(batch)} warning(s) since the last digest:\n{summary}",
            context={"conditions": [a.condition for a in batch]},
        )
        self._dispatch(digest)

    def _send_critical(self, alert: Alert) -> None:
        now = self._clock.now()
        last = self._last_sent.get(alert.condition)
        if last is not None and now - last < self._dedup_window:
            _logger.info("alert_deduped", condition=alert.condition)
            return
        self._last_sent[alert.condition] = now
        self._dispatch(alert)

    def _dispatch(self, alert: Alert) -> None:
        for channel in self._channels:
            try:
                channel.send(alert)
            except Exception as exc:
                _logger.error(
                    "alert_channel_send_failed",
                    channel=type(channel).__name__,
                    condition=alert.condition,
                    error=str(exc),
                )
