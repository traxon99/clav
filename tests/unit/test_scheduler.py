"""Story 5.4 — Scheduler wires an optional trade-review pass as its own job,
separate from scan_cycle/daily_reset, only when a TradeReviewService and an
interval are both configured; its wrapper never lets an exception escape,
matching the existing scan_cycle/daily_reset job wrappers."""

from __future__ import annotations

from clav.services.scheduler import Scheduler


class _FakeScanCycleService:
    def __init__(self) -> None:
        self.startup_reconcile_calls = 0

    def startup_reconcile(self) -> None:
        self.startup_reconcile_calls += 1

    def run(self, *, trigger: str) -> str:
        return "cycle-1"

    def daily_reset(self) -> None:
        pass


class _FakeReviewService:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.run_pass_calls = 0
        self._raises = raises

    def run_pass(self) -> None:
        self.run_pass_calls += 1
        if self._raises is not None:
            raise self._raises


def test_only_scan_and_daily_reset_jobs_when_review_not_configured() -> None:
    scheduler = Scheduler(_FakeScanCycleService(), scan_interval_minutes=30)
    scheduler.start()
    try:
        job_ids = {job.id for job in scheduler._scheduler.get_jobs()}
        assert job_ids == {"scan_cycle", "daily_reset"}
    finally:
        scheduler.shutdown()


def test_trade_review_job_registered_when_configured() -> None:
    scheduler = Scheduler(
        _FakeScanCycleService(),
        scan_interval_minutes=30,
        review_service=_FakeReviewService(),
        review_interval_minutes=120,
    )
    scheduler.start()
    try:
        job_ids = {job.id for job in scheduler._scheduler.get_jobs()}
        assert job_ids == {"scan_cycle", "daily_reset", "trade_review"}
    finally:
        scheduler.shutdown()


def test_review_pass_wrapper_catches_exceptions_like_the_other_jobs() -> None:
    review_service = _FakeReviewService(raises=RuntimeError("boom"))
    scheduler = Scheduler(
        _FakeScanCycleService(),
        scan_interval_minutes=30,
        review_service=review_service,
        review_interval_minutes=120,
    )
    scheduler.start()
    try:
        job = scheduler._scheduler.get_job("trade_review")
        job.func()  # what APScheduler would call on the interval -- must not raise
        assert review_service.run_pass_calls == 1
    finally:
        scheduler.shutdown()
