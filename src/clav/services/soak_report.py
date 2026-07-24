"""Soak-window summary for the go-live gate (Story 6.6, epic-06 decision #6):
a read-only, bounded query over the DB -- duplicate ``client_order_id``s
(must be zero), failed orders + stuck cycles, health/liveness status, and
daily-loss headroom. No live broker calls; a human reads this, alongside the
go-live checklist, before ever flipping ``mode: live``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from clav.data.repositories import Repositories
from clav.web.health_view import liveness_age_seconds, load_health_snapshot, overall_status

# A cycle started this many scan intervals ago and still lacking finished_at
# is treated as stuck/crashed rather than merely in-flight -- mirrors
# web/health_view.py's DEGRADED_CYCLE_MULTIPLE stance that a recently-started,
# still-running cycle is indeterminate, not evidence of a problem.
STUCK_CYCLE_INTERVAL_MULTIPLE = 2


def _aware(dt: datetime, reference: datetime) -> datetime:
    """SQLite round-trips can drop tzinfo; reattach ``reference``'s (same
    convention as ``web.health_view.liveness_age_seconds``) so downstream
    comparisons never raise on naive-vs-aware."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=reference.tzinfo)


@dataclass
class SoakReport:
    start: datetime
    end: datetime
    duplicate_client_order_ids: dict[str, int]
    failed_orders: int
    stuck_cycles: int
    cycles_run: int
    critical_health_events: int
    liveness_status: str
    liveness_age_seconds: float | None
    daily_pnl_pct: float | None
    daily_loss_cap_pct: float
    daily_loss_headroom_pct: float | None

    @property
    def duplicate_order_count(self) -> int:
        return sum(self.duplicate_client_order_ids.values())

    @property
    def unhandled_error_count(self) -> int:
        return self.failed_orders + self.stuck_cycles

    @property
    def clean(self) -> bool:
        """No duplicate orders, no unhandled errors, and current liveness is
        ok -- the report's one-line go/no-go for the human reading it before
        the go-live checklist (Story 6.6)."""
        return (
            self.duplicate_order_count == 0
            and self.unhandled_error_count == 0
            and self.liveness_status == "ok"
        )


def build_soak_report(
    repos: Repositories,
    *,
    start: datetime,
    end: datetime,
    now: datetime,
    scan_interval_minutes: int,
    max_daily_loss_pct: float,
) -> SoakReport:
    """Assemble a :class:`SoakReport` over ``[start, end]``. Every query is
    time-bounded and read-only; nothing here ever touches a broker. Renders
    cleanly on an empty window -- an install with no history yet reports zero
    counts and an ``ok``/indeterminate liveness rather than raising."""
    dup_ids = repos.orders.duplicate_client_order_ids(start=start, end=end)
    failed_orders = repos.orders.count_failed_since(start=start, end=end)

    cycles = repos.scan_cycles.list_since(start=start, end=end)
    stuck_cutoff = now - timedelta(minutes=scan_interval_minutes * STUCK_CYCLE_INTERVAL_MULTIPLE)
    stuck_cycles = sum(
        1
        for c in cycles
        if c.finished_at is None and _aware(c.started_at, now) <= stuck_cutoff
    )

    critical_health_events = repos.health_events.count_since(
        start=start, end=end, status="critical"
    )

    snapshot = load_health_snapshot(repos)
    last_cycle = repos.scan_cycles.latest()
    age_seconds = liveness_age_seconds(last_cycle, now)
    liveness_status = overall_status(snapshot, age_seconds, scan_interval_minutes)

    daily_pnl_event = repos.health_events.latest_by_name("trading", "daily_pnl_vs_cap")
    daily_pnl_pct: float | None = None
    headroom: float | None = None
    if daily_pnl_event is not None:
        daily_pnl_pct = daily_pnl_event.value.get("daily_pnl_pct")
        if daily_pnl_pct is not None:
            loss_pct = max(0.0, -daily_pnl_pct)
            headroom = max_daily_loss_pct - loss_pct

    return SoakReport(
        start=start,
        end=end,
        duplicate_client_order_ids=dup_ids,
        failed_orders=failed_orders,
        stuck_cycles=stuck_cycles,
        cycles_run=len(cycles),
        critical_health_events=critical_health_events,
        liveness_status=liveness_status,
        liveness_age_seconds=age_seconds,
        daily_pnl_pct=daily_pnl_pct,
        daily_loss_cap_pct=max_daily_loss_pct,
        daily_loss_headroom_pct=headroom,
    )


def format_soak_report(report: SoakReport) -> str:
    lines = [
        f"Soak report: {report.start.isoformat()} .. {report.end.isoformat()}",
        "",
        f"cycles run: {report.cycles_run}",
        f"duplicate client_order_ids: {report.duplicate_order_count}",
    ]
    if report.duplicate_client_order_ids:
        for client_order_id, count in report.duplicate_client_order_ids.items():
            lines.append(f"  - {client_order_id}: seen {count} times")
    lines += [
        f"failed orders: {report.failed_orders}",
        f"stuck cycles (started, never finished): {report.stuck_cycles}",
        f"unhandled errors (failed orders + stuck cycles): {report.unhandled_error_count}",
        f"critical health events: {report.critical_health_events}",
    ]
    liveness_detail = (
        f"{report.liveness_age_seconds:.0f}s since last completed cycle"
        if report.liveness_age_seconds is not None
        else "no completed cycle yet"
    )
    lines.append(f"liveness: {report.liveness_status} ({liveness_detail})")

    if report.daily_pnl_pct is not None and report.daily_loss_headroom_pct is not None:
        lines.append(
            f"daily P&L: {report.daily_pnl_pct * 100:.2f}% "
            f"(cap {report.daily_loss_cap_pct * 100:.2f}%, "
            f"headroom {report.daily_loss_headroom_pct * 100:.2f}%)"
        )
    else:
        lines.append(f"daily P&L: n/a (cap {report.daily_loss_cap_pct * 100:.2f}%)")

    lines.append("")
    lines.append(
        "CLEAN -- ready for go-live checklist review"
        if report.clean
        else "NOT CLEAN -- resolve the findings above before the go-live checklist"
    )
    return "\n".join(lines)
