"""Story 4.9 — descriptive calibration view: joins **closed** trades to the
``decision``/``analysis_result`` that drove them, so a stakeholder can see
whether high-conviction Gemini calls actually paid off ahead of the full
Epic-5 review. Explicitly descriptive (epic decision #6) -- it reads
existing rows and adds no scored calibration model or review worker; the
structured retrospective is Epic 5."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clav.data.repositories import Repositories
from clav.web.charts import scatter_svg

# Bounds how much trade history a single request pulls, regardless of how
# large the trade table has grown (Pi RAM discipline).
MAX_TRADES = 500

# Conviction bands, keyed by |conviction| -- the LLM's reported conviction is
# a signed strength-of-evidence score ([-1, 1]), so bucketing by magnitude
# groups "the analyst was confident" regardless of direction.
_BANDS: list[tuple[float, float, str]] = [
    (0.0, 0.25, "0.0-0.25"),
    (0.25, 0.5, "0.25-0.5"),
    (0.5, 0.75, "0.5-0.75"),
    (0.75, 1.01, "0.75-1.0"),
]


def _band_label(conviction: float) -> str:
    magnitude = abs(conviction)
    for lo, hi, label in _BANDS:
        if lo <= magnitude < hi:
            return label
    return _BANDS[-1][2]


@dataclass
class BucketStats:
    label: str
    count: int
    mean_return_pct: float | None
    hit_rate: float | None


def _summarize(returns_pct: list[float], wins: list[bool]) -> tuple[float | None, float | None]:
    """Mean return and hit-rate over a sample, or ``(None, None)`` for an
    empty sample rather than dividing by zero."""
    if not returns_pct:
        return None, None
    mean = sum(returns_pct) / len(returns_pct)
    hit_rate = sum(1 for w in wins if w) / len(wins)
    return mean, hit_rate


def build_calibration_view(repos: Repositories) -> dict[str, Any]:
    trades = repos.trades.list_closed(limit=MAX_TRADES)

    scatter_points: list[tuple[float, float]] = []
    band_returns: dict[str, list[float]] = {label: [] for _, _, label in _BANDS}
    band_wins: dict[str, list[bool]] = {label: [] for _, _, label in _BANDS}
    gemini_returns: list[float] = []
    gemini_wins: list[bool] = []
    technical_returns: list[float] = []
    technical_wins: list[bool] = []

    for trade in trades:
        if trade.realized_pl is None or trade.return_pct is None or trade.entry_decision_id is None:
            continue
        decision = repos.decisions.get(trade.entry_decision_id)
        llm = (decision.reasoning or {}).get("llm") if decision is not None else None
        win = trade.realized_pl >= 0

        if llm is not None and llm.get("conviction") is not None:
            conviction = float(llm["conviction"])
            scatter_points.append((conviction, trade.realized_pl))
            label = _band_label(conviction)
            band_returns[label].append(trade.return_pct)
            band_wins[label].append(win)
            gemini_returns.append(trade.return_pct)
            gemini_wins.append(win)
        else:
            technical_returns.append(trade.return_pct)
            technical_wins.append(win)

    buckets = [
        BucketStats(
            label=label,
            count=len(band_returns[label]),
            mean_return_pct=_summarize(band_returns[label], band_wins[label])[0],
            hit_rate=_summarize(band_returns[label], band_wins[label])[1],
        )
        for _, _, label in _BANDS
    ]

    gemini_mean, gemini_hit_rate = _summarize(gemini_returns, gemini_wins)
    technical_mean, technical_hit_rate = _summarize(technical_returns, technical_wins)

    return {
        "sample_count": len(gemini_returns) + len(technical_returns),
        "gemini_count": len(gemini_returns),
        "technical_count": len(technical_returns),
        "gemini_mean_return_pct": gemini_mean,
        "gemini_hit_rate": gemini_hit_rate,
        "technical_mean_return_pct": technical_mean,
        "technical_hit_rate": technical_hit_rate,
        "buckets": buckets,
        "scatter_svg": scatter_svg(scatter_points),
    }
