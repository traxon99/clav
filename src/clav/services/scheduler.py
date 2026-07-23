"""APScheduler wiring: scan cycle on an interval during configured hours,
startup reconciliation, the daily reset job (Story 1.13), and the trade-review
pass (Story 5.4)."""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from clav.common.logging import get_logger
from clav.services.review import TradeReviewService
from clav.services.scan_cycle import ScanCycleService

_logger = get_logger(__name__)


class Scheduler:
    def __init__(
        self,
        scan_cycle_service: ScanCycleService,
        *,
        scan_interval_minutes: int,
        review_service: TradeReviewService | None = None,
        review_interval_minutes: int | None = None,
    ) -> None:
        self._service = scan_cycle_service
        self._scan_interval_minutes = scan_interval_minutes
        self._review_service = review_service
        self._review_interval_minutes = review_interval_minutes
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
        # Own job, own interval -- deliberately separate from scan_cycle/
        # daily_reset (Story 5.4) so a slow or backlogged review pass can
        # never delay a scan cycle. Only registered when both a service and
        # an interval are configured, so a caller that hasn't wired Epic 5
        # yet (or a test using only ScanCycleService) is unaffected.
        if self._review_service is not None and self._review_interval_minutes is not None:
            review_service = self._review_service
            review_interval_minutes = self._review_interval_minutes

            def _run_review_pass() -> None:
                try:
                    review_service.run_pass()
                except Exception:
                    _logger.exception("scheduled_trade_review_pass_failed")

            self._scheduler.add_job(
                _run_review_pass,
                IntervalTrigger(minutes=review_interval_minutes),
                id="trade_review",
                max_instances=1,
                coalesce=True,
            )

        self._scheduler.start()
        _logger.info(
            "scheduler_started",
            scan_interval_minutes=self._scan_interval_minutes,
            review_interval_minutes=self._review_interval_minutes,
        )

    def shutdown(self) -> None:
        self._scheduler.shutdown()

    def _run_cycle(self) -> None:
        try:
            self._service.run(trigger="scheduled")
        except Exception:
            _logger.exception("scheduled_scan_cycle_failed")
            return
        self._maybe_reschedule_scan_interval()

    def _maybe_reschedule_scan_interval(self) -> None:
        """Live-apply a Story-3.8 ``scan_interval_minutes`` operator override
        (e.g. the analysis-effort preset toggle) without a clav-core restart.
        ``ScanCycleService`` sets ``last_scan_interval_override`` each cycle
        it runs (None ⇒ no override, or no runtime_config wired at all ⇒
        leave the boot-config cadence alone). Skipped-cycle ticks (market
        closed) never reach here, so during closed hours a fresh override
        only takes effect once the market reopens and a real cycle runs."""
        minutes = getattr(self._service, "last_scan_interval_override", None)
        if minutes is None or minutes == self._scan_interval_minutes:
            return
        self._scan_interval_minutes = minutes
        self._scheduler.reschedule_job("scan_cycle", trigger=IntervalTrigger(minutes=minutes))
        _logger.info("scan_interval_rescheduled", scan_interval_minutes=minutes)

    def _daily_reset(self) -> None:
        try:
            self._service.daily_reset()
        except Exception:
            _logger.exception("daily_reset_failed")
