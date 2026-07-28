"""Am I beating the market? — pure, DB-free comparison of portfolio vs benchmark.

Both sides are normalized to their own starting value, so the answer is
"since I started, my equity is up X% and VOO is up Y%, so I'm ahead/behind by
X-Y points". Cash is included at face value, deliberately: ``equity`` is
cash + position market value, so sitting 60% in cash means rising at 40% of a
fully-invested pace. That is the honest answer to "should I have just bought
the index instead", which is the question being asked -- cash drag counts
against you because holding cash was a choice.

(The other question -- "are my *picks* good, ignoring allocation?" -- needs a
cash-matched benchmark and is deliberately not answered here.)

One caveat this cannot see: an **external cash flow**. ``equity`` rising because
money was deposited is indistinguishable here from equity rising because the
strategy worked, and would render as a spectacular fake outperformance. CLAV has
no deposit tracking today and a paper account funded once never has one, so the
simple ratio is correct as things stand -- but ``BenchmarkComparison`` carries
the starting equity so a caller can sanity-check it, and this docstring is the
warning for whoever adds funding later. The fix at that point is a time-weighted
return (segment the series at each cash flow, chain-link the sub-period returns),
not a patch to this function.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["BenchmarkComparison", "compare_to_benchmark"]


@dataclass(frozen=True)
class BenchmarkComparison:
    symbol: str
    portfolio_return_pct: float
    benchmark_return_pct: float
    #: Percentage points ahead (+) or behind (-) the benchmark.
    delta_pct: float
    starting_equity: float
    current_equity: float

    @property
    def beating(self) -> bool:
        return self.delta_pct > 0


def compare_to_benchmark(
    *,
    symbol: str,
    starting_equity: float,
    current_equity: float,
    starting_price: float,
    current_price: float,
) -> BenchmarkComparison | None:
    """None when either series lacks a usable baseline -- a zero/negative
    starting equity or price has no meaningful percentage change, and the tile
    should say "not enough history" rather than render a fabricated 0%."""
    if starting_equity <= 0 or starting_price <= 0:
        return None

    portfolio_pct = (current_equity - starting_equity) / starting_equity * 100.0
    benchmark_pct = (current_price - starting_price) / starting_price * 100.0
    return BenchmarkComparison(
        symbol=symbol.upper(),
        portfolio_return_pct=round(portfolio_pct, 2),
        benchmark_return_pct=round(benchmark_pct, 2),
        delta_pct=round(portfolio_pct - benchmark_pct, 2),
        starting_equity=starting_equity,
        current_equity=current_equity,
    )
