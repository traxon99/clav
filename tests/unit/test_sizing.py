"""Story 2.3: PositionSizer table tests — ATR-based sizing/stops, each budget
clamp exercised in isolation, and the ATR-unavailable flat fallback."""

import math

import pytest

from clav.domain.risk.sizing import PositionSizer, SizingBudgets

UNCONSTRAINED = SizingBudgets(
    max_position_value=float("inf"),
    remaining_exposure_budget=float("inf"),
    remaining_sector_budget=float("inf"),
    buying_power=float("inf"),
)


def _sizer(**overrides) -> PositionSizer:
    defaults = dict(
        risk_fraction=0.01, atr_stop_mult=2.0, take_profit_mult=2.0, default_order_value=1000.0
    )
    defaults.update(overrides)
    return PositionSizer(**defaults)


def test_doubling_atr_halves_raw_qty() -> None:
    sizer = _sizer(risk_fraction=0.01)
    equity = 100_000.0
    price = 100.0

    result_1x = sizer.size(equity=equity, price=price, atr_14=1.0, budgets=UNCONSTRAINED)
    result_2x = sizer.size(equity=equity, price=price, atr_14=2.0, budgets=UNCONSTRAINED)

    assert result_1x.used_atr is True
    assert result_2x.used_atr is True
    # risk_per_trade=1000, stop_distance=atr*2 -> raw_qty = 1000/(atr*2)
    assert result_1x.qty == 500  # 1000 / 2.0
    assert result_2x.qty == 250  # 1000 / 4.0 -- exactly half of the 1x case
    assert result_2x.qty == result_1x.qty // 2


def test_stop_and_take_profit_computed_from_atr() -> None:
    sizer = _sizer(atr_stop_mult=2.0, take_profit_mult=3.0)
    result = sizer.size(equity=100_000.0, price=100.0, atr_14=1.0, budgets=UNCONSTRAINED)

    stop_distance = 1.0 * 2.0
    assert result.stop_price == pytest.approx(100.0 - stop_distance)
    assert result.take_profit_price == pytest.approx(100.0 + stop_distance * 3.0)


@pytest.mark.parametrize(
    ("field", "value", "expected_qty"),
    [
        ("max_position_value", 500.0, 5),  # 500 / 100 price
        ("remaining_exposure_budget", 300.0, 3),
        ("remaining_sector_budget", 200.0, 2),
        ("buying_power", 150.0, 1),
    ],
)
def test_each_budget_clamp_exercised_in_isolation(field, value, expected_qty) -> None:
    # risk_fraction sized generously so the budget, not ATR risk, is the binding clamp.
    sizer = _sizer(risk_fraction=1.0, atr_stop_mult=1.0)
    budgets_kwargs = {
        "max_position_value": float("inf"),
        "remaining_exposure_budget": float("inf"),
        "remaining_sector_budget": float("inf"),
        "buying_power": float("inf"),
    }
    budgets_kwargs[field] = value
    budgets = SizingBudgets(**budgets_kwargs)

    result = sizer.size(equity=1_000_000.0, price=100.0, atr_14=1.0, budgets=budgets)

    assert result.qty == expected_qty


def test_atr_missing_falls_back_to_flat_default_order_value_with_no_stop() -> None:
    sizer = _sizer(default_order_value=1000.0)
    result = sizer.size(equity=100_000.0, price=100.0, atr_14=None, budgets=UNCONSTRAINED)

    assert result.used_atr is False
    assert result.qty == math.floor(1000.0 / 100.0)
    assert result.stop_price is None
    assert result.take_profit_price is None
    assert result.notes["reason"] == "atr_unavailable_flat_fallback"


def test_atr_zero_or_negative_also_triggers_fallback() -> None:
    sizer = _sizer()
    result = sizer.size(equity=100_000.0, price=100.0, atr_14=0.0, budgets=UNCONSTRAINED)
    assert result.used_atr is False


def test_flat_fallback_still_respects_budgets() -> None:
    sizer = _sizer(default_order_value=10_000.0)
    budgets = SizingBudgets(
        max_position_value=200.0,
        remaining_exposure_budget=float("inf"),
        remaining_sector_budget=float("inf"),
        buying_power=float("inf"),
    )
    result = sizer.size(equity=100_000.0, price=100.0, atr_14=None, budgets=budgets)

    assert result.qty == 2  # clamped by max_position_value, not the flat 100 shares


def test_qty_never_negative_or_fractional() -> None:
    sizer = _sizer()
    budgets = SizingBudgets(
        max_position_value=1.0,  # can't even afford one share at price=100
        remaining_exposure_budget=float("inf"),
        remaining_sector_budget=float("inf"),
        buying_power=float("inf"),
    )
    result = sizer.size(equity=100_000.0, price=100.0, atr_14=1.0, budgets=budgets)

    assert result.qty == 0
    assert isinstance(result.qty, int)
    assert result.stop_price is None
    assert result.take_profit_price is None


def test_exhausted_exposure_budget_sizes_to_zero_not_negative() -> None:
    sizer = _sizer()
    budgets = SizingBudgets(
        max_position_value=float("inf"),
        remaining_exposure_budget=-500.0,  # already over the exposure cap
        remaining_sector_budget=float("inf"),
        buying_power=float("inf"),
    )
    result = sizer.size(equity=100_000.0, price=100.0, atr_14=1.0, budgets=budgets)

    assert result.qty == 0


def test_invalid_price_sizes_to_zero() -> None:
    sizer = _sizer()
    result = sizer.size(equity=100_000.0, price=0.0, atr_14=1.0, budgets=UNCONSTRAINED)
    assert result.qty == 0
    assert result.used_atr is False
