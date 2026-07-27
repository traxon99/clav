"""The dashboard's Robinhood-style portfolio-value hero: a big current-equity
number, a signed change (absolute + %) against a selectable lookback period,
and a color-coded (gain=green, loss=red) equity sparkline for that period.
Purely descriptive over the already-persisted ``portfolio_snapshot`` history
-- no new capture plumbing, and every query stays bounded regardless of how
far back the period reaches (Pi RAM discipline, same convention as every
other Epic 4 dashboard view)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from clav.data.repositories import Repositories
from clav.web.charts import interactive_line_chart, sparkline_svg

_MARKET_TZ = "America/New_York"
_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(16, 0)


def _is_market_hours(ts: datetime) -> bool:
    """Regular NYSE/NASDAQ session, Mon-Fri 9:30-16:00 ET -- a simple
    weekday + time-of-day approximation (no holiday calendar; snapshots are
    captured around the clock regardless of trading hours). Good enough to
    keep the chart's line continuous across sessions instead of dragging a
    flat dead-time segment through each night/weekend."""
    if ts.weekday() >= 5:
        return False
    local = ts.replace(tzinfo=UTC).astimezone(ZoneInfo(_MARKET_TZ)).time()
    return _MARKET_OPEN <= local <= _MARKET_CLOSE


# Bounds how many snapshots a single period's chart can pull, regardless of
# how far back the period reaches (a year of frequent snapshots could
# otherwise be unbounded).
MAX_PERIOD_SNAPSHOTS = 500

GAIN_COLOR = "#1a7a34"
LOSS_COLOR = "#b02a2a"

DEFAULT_PERIOD = "1d"

_PERIOD_LABELS: dict[str, str] = {
    "1h": "1H",
    "1d": "1D",
    "1w": "1W",
    "ytd": "YTD",
    "1y": "1Y",
}


def _period_cutoff(period: str, now: datetime) -> datetime:
    if period == "1h":
        return now - timedelta(hours=1)
    if period == "1d":
        return now - timedelta(days=1)
    if period == "1w":
        return now - timedelta(weeks=1)
    if period == "ytd":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "1y":
        return now - timedelta(days=365)
    raise ValueError(f"unknown period: {period!r}")


def build_portfolio_value_view(repos: Repositories, now: datetime, period: str) -> dict[str, Any]:
    period = period if period in _PERIOD_LABELS else DEFAULT_PERIOD
    periods = [
        {"key": key, "label": label, "active": key == period}
        for key, label in _PERIOD_LABELS.items()
    ]

    latest = repos.portfolio_snapshots.latest()
    if latest is None:
        return {
            "has_data": False,
            "period": period,
            "period_label": _PERIOD_LABELS[period],
            "periods": periods,
            "chart_svg": sparkline_svg([]),
        }

    cutoff = _period_cutoff(period, now)
    since_rows = repos.portfolio_snapshots.get_since(cutoff, limit=MAX_PERIOD_SNAPSHOTS)
    baseline_row = repos.portfolio_snapshots.get_last_before(cutoff)

    if baseline_row is not None:
        baseline_equity = baseline_row.equity
    elif since_rows:
        # No snapshot old enough to anchor the period -- history doesn't go
        # back that far yet, so the earliest point we have is the best
        # available baseline (never a crash/None).
        baseline_equity = since_rows[0].equity
    else:
        baseline_equity = latest.equity

    change_abs = latest.equity - baseline_equity
    change_pct = (change_abs / baseline_equity) if baseline_equity else None

    # Only market-hours points get plotted (``_is_market_hours``), so the
    # line runs continuously trading-session to trading-session instead of
    # dragging a flat dead-time segment through each night/weekend -- rows
    # and the ``cutoff``/``now`` anchors alike are dropped when off-hours.
    # If that leaves fewer than 2 points (thin history, or the whole period
    # happens to be off-hours), fall back to anchoring unconditionally at
    # ``cutoff``/``now`` so the chart still spans the period rather than
    # collapsing to the "not enough data" empty state. Snapshot timestamps
    # come back tz-naive from SQLite; ``cutoff`` and ``now`` are tz-aware
    # (derived from the injected clock), so both are stripped to match
    # before sharing a sort key with real rows.
    _tsfmt = "%b %d, %H:%M"
    market_rows = [row for row in since_rows if _is_market_hours(row.ts)]
    points: dict[datetime, float] = {}
    if _is_market_hours(cutoff):
        points[cutoff.replace(tzinfo=None)] = baseline_equity
    if _is_market_hours(now):
        points[now.replace(tzinfo=None)] = latest.equity
    points.update((row.ts, row.equity) for row in market_rows)

    if len(points) < 2:
        # Thin/off-hours history -- fall back to the unconditional anchors
        # (pre-market-filtering behavior) so the chart still renders instead
        # of collapsing to "not enough data".
        points = {
            cutoff.replace(tzinfo=None): baseline_equity,
            now.replace(tzinfo=None): latest.equity,
        }
        points.update((row.ts, row.equity) for row in since_rows)

    ordered = sorted(points.items())
    values = [equity for _, equity in ordered]
    labels = [ts.strftime(_tsfmt) for ts, _ in ordered]

    return {
        "has_data": True,
        "current_equity": latest.equity,
        "as_of": latest.ts,
        "change_abs": change_abs,
        "change_pct": change_pct,
        "is_gain": change_abs >= 0,
        "chart_svg": interactive_line_chart(
            values,
            labels,
            width=640,
            height=180,
            stroke=GAIN_COLOR if change_abs >= 0 else LOSS_COLOR,
            value_prefix="$",
        ),
        "period": period,
        "period_label": _PERIOD_LABELS[period],
        "periods": periods,
    }
