"""APScheduler wiring: scan cycle on an interval during configured hours,
startup reconciliation, and the daily reset job (Story 1.13)."""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from clav.common.logging import get_logger
from clav.services.scan_cycle import ScanCycleService

_logger = get_logger(__name__)


class Scheduler:
    def __init__(self, scan_cycle_service: ScanCycleService, *, scan_interval_minutes: int) -> None:
        self._service = scan_cycle_service
        self._scan_interval_minutes = scan_interval_minutes
        self._scheduler = BackgroundScheduler()

    def start(self) -> None:
        _logger.info("startup_reconciliation_begin")
        self._service.startup_reconcile()
        _logger.info("startup_reconciliation_complete")

        self._scheduler.add_job(
            self._run_cycle,
            IntervalTrigger(minutes=self._scan_interval_minutes),
            id="scan_cycle",
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.add_job(
            self._daily_reset,
            CronTrigger(hour=0, minute=0),
            id="daily_reset",
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.start()
        _logger.info("scheduler_started", scan_interval_minutes=self._scan_interval_minutes)

    def shutdown(self) -> None:
        self._scheduler.shutdown()

    def _run_cycle(self) -> None:
        try:
            self._service.run(trigger="scheduled")
        except Exception:
            _logger.exception("scheduled_scan_cycle_failed")

    def _daily_reset(self) -> None:
        try:
            self._service.daily_reset()
        except Exception:
            _logger.exception("daily_reset_failed")
