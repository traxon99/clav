"""Story 2.3 — PositionSizer: ATR-based sizing, budget clamps, and the
flat-fallback when ATR is unavailable. Table-driven per the epic's acceptance
criteria (docs/epics/epic-02-risk-and-portfolio.md, Story 2.3)."""

from __future__ import annotations

import pytest

from clav.domain.risk.sizing import PositionSizer, SizingBudgets

GENEROUS_BUDGETS = SizingBudgets(
    max_position_value=1_000_000.0,
    remaining_exposure_budget=1_000_000.0,
    remaining_sector_budget=1_000_000.0,
    buying_power=1_000_000.0,
)


def _sizer(
    *,
    risk_fraction: float = 0.01,
    atr_stop_mult: float = 2.0,
    take_profit_mult: float = 2.0,
    default_order_value: float = 1000.0,
) -> PositionSizer:
    return PositionSizer(
        risk_fraction=risk_fraction,
        atr_stop_mult=atr_stop_mult,
        take_profit_mult=take_profit_mult,
        default_order_value=default_order_value,
    )


# --- ATR-based sizing, unclamped -------------------------------------------


def test_atr_sizing_matches_the_formula() -> None:
    # risk_per_trade = 100_000 * 0.01 = 1000; stop_distance = 5 * 2 = 10; raw_qty = 100
    result = _sizer().size(equity=100_000.0, price=50.0, atr_14=5.0, budgets=GENEROUS_BUDGETS)
    assert result.qty == 100
    assert result.sized_by == "atr"
    assert result.stop_price == pytest.approx(40.0)
    assert result.take_profit_price == pytest.approx(70.0)  # 50 + 10*2


def test_doubling_atr_halves_raw_qty() -> None:
    base = _sizer().size(equity=100_000.0, price=50.0, atr_14=5.0, budgets=GENEROUS_BUDGETS)
    doubled = _sizer().size(equity=100_000.0, price=50.0, atr_14=10.0, budgets=GENEROUS_BUDGETS)
    assert doubled.qty == pytest.approx(base.qty / 2)


def test_stop_and_take_profit_scale_with_atr_stop_mult_and_take_profit_mult() -> None:
    result = _sizer(atr_stop_mult=3.0, take_profit_mult=1.5).size(
        equity=100_000.0, price=100.0, atr_14=4.0, budgets=GENEROUS_BUDGETS
    )
    # stop_distance = 4 * 3 = 12
    assert result.stop_price == pytest.approx(88.0)
    assert result.take_profit_price == pytest.approx(100.0 + 12 * 1.5)


# --- budget clamps, exercised in isolation ---------------------------------


def test_max_position_value_clamp() -> None:
    budgets = SizingBudgets(
        max_position_value=500.0,  # tightest: caps to 5 shares @ 100
        remaining_exposure_budget=1_000_000.0,
        remaining_sector_budget=1_000_000.0,
        buying_power=1_000_000.0,
    )
    # unclamped raw_qty would be huge: risk_per_trade=1000, stop_distance=1 -> 1000
    result = _sizer().size(equity=100_000.0, price=100.0, atr_14=0.5, budgets=budgets)
    assert result.qty == 5


def test_remaining_exposure_budget_clamp() -> None:
    budgets = SizingBudgets(
        max_position_value=1_000_000.0,
        remaining_exposure_budget=300.0,  # tightest: caps to 3 shares @ 100
        remaining_sector_budget=1_000_000.0,
        buying_power=1_000_000.0,
    )
    result = _sizer().size(equity=100_000.0, price=100.0, atr_14=0.5, budgets=budgets)
    assert result.qty == 3


def test_remaining_sector_budget_clamp() -> None:
    budgets = SizingBudgets(
        max_position_value=1_000_000.0,
        remaining_exposure_budget=1_000_000.0,
        remaining_sector_budget=200.0,  # tightest: caps to 2 shares @ 100
        buying_power=1_000_000.0,
    )
    result = _sizer().size(equity=100_000.0, price=100.0, atr_14=0.5, budgets=budgets)
    assert result.qty == 2


def test_buying_power_clamp() -> None:
    budgets = SizingBudgets(
        max_position_value=1_000_000.0,
        remaining_exposure_budget=1_000_000.0,
        remaining_sector_budget=1_000_000.0,
        buying_power=150.0,  # tightest: caps to 1 share @ 100
    )
    result = _sizer().size(equity=100_000.0, price=100.0, atr_14=0.5, budgets=budgets)
    assert result.qty == 1


def test_a_negative_remaining_budget_clamps_to_zero_not_negative() -> None:
    budgets = SizingBudgets(
        max_position_value=1_000_000.0,
        remaining_exposure_budget=-500.0,  # already over the exposure cap
        remaining_sector_budget=1_000_000.0,
        buying_power=1_000_000.0,
    )
    result = _sizer().size(equity=100_000.0, price=100.0, atr_14=0.5, budgets=budgets)
    assert result.qty == 0
    assert result.stop_price is None
    assert result.take_profit_price is None


# --- ATR-missing fallback ---------------------------------------------------


def test_atr_missing_falls_back_to_flat_sizing_with_no_stop() -> None:
    result = _sizer(default_order_value=1000.0).size(
        equity=100_000.0, price=100.0, atr_14=None, budgets=GENEROUS_BUDGETS
    )
    assert result.qty == 10  # floor(1000 / 100)
    assert result.sized_by == "flat"
    assert result.stop_price is None
    assert result.take_profit_price is None


def test_atr_zero_or_negative_also_falls_back_to_flat_sizing() -> None:
    result = _sizer(default_order_value=1000.0).size(
        equity=100_000.0, price=100.0, atr_14=0.0, budgets=GENEROUS_BUDGETS
    )
    assert result.sized_by == "flat"
    assert result.qty == 10


def test_flat_fallback_still_respects_budget_clamps() -> None:
    budgets = SizingBudgets(
        max_position_value=250.0,  # tighter than the flat 1000/100=10 shares
        remaining_exposure_budget=1_000_000.0,
        remaining_sector_budget=1_000_000.0,
        buying_power=1_000_000.0,
    )
    result = _sizer(default_order_value=1000.0).size(
        equity=100_000.0, price=100.0, atr_14=None, budgets=budgets
    )
    assert result.qty == 2  # floor(250/100)
    assert result.sized_by == "flat"


# --- never negative/fractional; <=0 reads as "no entry" --------------------


def test_invalid_price_sizes_to_zero() -> None:
    result = _sizer().size(equity=100_000.0, price=0.0, atr_14=5.0, budgets=GENEROUS_BUDGETS)
    assert result.qty == 0
    assert result.sized_by == "none"


def test_qty_is_always_an_int_never_fractional() -> None:
    result = _sizer().size(equity=100_333.0, price=33.0, atr_14=1.7, budgets=GENEROUS_BUDGETS)
    assert isinstance(result.qty, int)
