"""Story 5.4 — Scheduler wires an optional trade-review pass as its own job,
separate from scan_cycle/daily_reset, only when a TradeReviewService and an
interval are both configured; its wrapper never lets an exception escape,
matching the existing scan_cycle/daily_reset job wrappers."""

from __future__ import annotations

from datetime import timedelta

from clav.services.scheduler import Scheduler


class _FakeScanCycleService:
    def __init__(self, *, last_scan_interval_override: int | None = None) -> None:
        self.startup_reconcile_calls = 0
        self.last_scan_interval_override = last_scan_interval_override

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


def test_scan_interval_override_reschedules_the_job_live() -> None:
    """The analysis-effort preset toggle (Fast=10min/Thoughtful=30min) must
    take effect on the very next executed cycle -- no clav-core restart."""
    service = _FakeScanCycleService(last_scan_interval_override=10)
    scheduler = Scheduler(service, scan_interval_minutes=30)
    scheduler.start()
    try:
        scheduler._run_cycle()
        job = scheduler._scheduler.get_job("scan_cycle")
        assert job.trigger.interval == timedelta(minutes=10)
        assert scheduler._scan_interval_minutes == 10
    finally:
        scheduler.shutdown()


def test_no_override_leaves_the_schedule_untouched() -> None:
    service = _FakeScanCycleService(last_scan_interval_override=None)
    scheduler = Scheduler(service, scan_interval_minutes=30)
    scheduler.start()
    try:
        scheduler._run_cycle()
        job = scheduler._scheduler.get_job("scan_cycle")
        assert job.trigger.interval == timedelta(minutes=30)
    finally:
        scheduler.shutdown()


def test_matching_override_does_not_needlessly_reschedule() -> None:
    """An override equal to the current cadence is a no-op -- rescheduling
    would reset next_run_time to now+interval for no reason."""
    service = _FakeScanCycleService(last_scan_interval_override=30)
    scheduler = Scheduler(service, scan_interval_minutes=30)
    scheduler.start()
    try:
        job_before = scheduler._scheduler.get_job("scan_cycle")
        next_run_before = job_before.next_run_time
        scheduler._run_cycle()
        job_after = scheduler._scheduler.get_job("scan_cycle")
        assert job_after.next_run_time == next_run_before
    finally:
        scheduler.shutdown()


def test_a_failed_cycle_does_not_attempt_to_reschedule() -> None:
    class _RaisingService(_FakeScanCycleService):
        def run(self, *, trigger: str) -> str:
            raise RuntimeError("boom")

    service = _RaisingService(last_scan_interval_override=10)
    scheduler = Scheduler(service, scan_interval_minutes=30)
    scheduler.start()
    try:
        scheduler._run_cycle()  # must not raise
        job = scheduler._scheduler.get_job("scan_cycle")
        assert job.trigger.interval == timedelta(minutes=30)  # unchanged
    finally:
        scheduler.shutdown()
