"""The dashboard's Robinhood-style portfolio-value hero: a big current-equity
number, a signed change (absolute + %) against a selectable lookback period,
and a color-coded (gain=green, loss=red) equity sparkline for that period.
Purely descriptive over the already-persisted ``portfolio_snapshot`` history
-- no new capture plumbing, and every query stays bounded regardless of how
far back the period reaches (Pi RAM discipline, same convention as every
other Epic 4 dashboard view)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from clav.data.repositories import Repositories
from clav.web.charts import interactive_line_chart, sparkline_svg

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

    # Anchor the line at the period start (``cutoff``, or ``baseline_row``'s
    # own timestamp when older history exists) and at the latest snapshot,
    # so the chart always spans the full selected period -- flat wherever
    # nothing happened yet -- instead of only plotting actual snapshot rows
    # and falling back to the "not enough data" empty state when there's
    # just one.
    # Snapshot timestamps come back tz-naive from SQLite; ``cutoff`` is
    # derived from the (tz-aware) injected clock, so it's stripped to match
    # before it can share a sort key with real rows.
    _tsfmt = "%b %d, %H:%M"
    start_ts = baseline_row.ts if baseline_row is not None else cutoff.replace(tzinfo=None)
    points: dict[datetime, float] = {start_ts: baseline_equity}
    points.update((row.ts, row.equity) for row in since_rows)
    points[latest.ts] = latest.equity

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
