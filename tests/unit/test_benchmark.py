"""Domain: "am I beating the market" comparison (portfolio vs index)."""

from __future__ import annotations

from clav.domain.benchmark import compare_to_benchmark


def _cmp(**kw):
    base = dict(
        symbol="voo",
        starting_equity=10_000.0,
        current_equity=11_000.0,
        starting_price=400.0,
        current_price=420.0,
    )
    return compare_to_benchmark(**{**base, **kw})


def test_beating_the_benchmark() -> None:
    c = _cmp()
    assert c is not None
    assert c.symbol == "VOO"  # normalized
    assert c.portfolio_return_pct == 10.0
    assert c.benchmark_return_pct == 5.0
    assert c.delta_pct == 5.0
    assert c.beating is True


def test_losing_to_the_benchmark() -> None:
    c = _cmp(current_equity=10_200.0)
    assert c is not None
    assert c.portfolio_return_pct == 2.0
    assert c.delta_pct == -3.0
    assert c.beating is False


def test_cash_drag_is_counted_against_you_not_removed() -> None:
    """Half in cash, and the invested half exactly tracks the index: you are
    behind. That is correct -- "should I have just bought VOO" includes the
    cost of not being invested."""
    # 10k start: 5k cash + 5k invested. Index +10% -> invested half -> 5.5k.
    c = _cmp(current_equity=10_500.0, starting_price=400.0, current_price=440.0)
    assert c is not None
    assert c.portfolio_return_pct == 5.0
    assert c.benchmark_return_pct == 10.0
    assert c.delta_pct == -5.0


def test_flat_benchmark_still_reports_portfolio_gain() -> None:
    c = _cmp(current_price=400.0)
    assert c is not None
    assert c.benchmark_return_pct == 0.0
    assert c.delta_pct == 10.0


def test_no_baseline_returns_none_rather_than_a_fake_zero() -> None:
    """A brand-new install has no history; the tile must say so, not print 0%."""
    assert _cmp(starting_equity=0.0) is None
    assert _cmp(starting_price=0.0) is None
    assert _cmp(starting_equity=-5.0) is None
