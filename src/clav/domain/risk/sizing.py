"""PositionSizer — volatility-aware (ATR) position sizing with stop-loss /
take-profit computed at entry (Story 2.3, docs/06-safety-and-risk.md §3).

A pure function of (equity, price, atr_14, budgets): no I/O, no Clock, no
repos — the same "Strategy" shape as ``RiskRule``/``DecisionEngine``
(docs/05-class-design.md §2), so it's deterministic and trivially
table-testable. Threading its ``qty`` into the risk-evaluation pipeline
itself (replacing the flat ``target_qty`` the Epic-1 ``DecisionEngine``
still emits) is Story 2.10's job; today its output drives the ATR-based
stop-loss/take-profit that ``PortfolioManager`` persists on a new position.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class SizingBudgets:
    """Remaining USD headroom for each independent cap the sizer clamps
    against (docs/06-safety-and-risk.md §3). Pass ``float("inf")`` for a
    budget that shouldn't constrain this call (e.g. the sector budget
    before Story 2.6 tags any instrument)."""

    max_position_value: float
    remaining_exposure_budget: float
    remaining_sector_budget: float
    buying_power: float


class SizingResult(BaseModel):
    qty: int
    stop_price: float | None = None
    take_profit_price: float | None = None
    used_atr: bool
    notes: dict[str, str] = Field(default_factory=dict)


class PositionSizer:
    def __init__(
        self,
        *,
        risk_fraction: float,
        atr_stop_mult: float,
        take_profit_mult: float,
        default_order_value: float,
    ) -> None:
        self._risk_fraction = risk_fraction
        self._atr_stop_mult = atr_stop_mult
        self._take_profit_mult = take_profit_mult
        self._default_order_value = default_order_value

    def size(
        self,
        *,
        equity: float,
        price: float,
        atr_14: float | None,
        budgets: SizingBudgets,
    ) -> SizingResult:
        if price <= 0:
            return SizingResult(qty=0, used_atr=False, notes={"reason": "invalid_price"})

        budget_qty = self._max_qty_from_budgets(price, budgets)

        if atr_14 is None or atr_14 <= 0:
            qty = min(math.floor(self._default_order_value / price), budget_qty)
            return SizingResult(
                qty=max(math.floor(qty), 0),
                used_atr=False,
                notes={"reason": "atr_unavailable_flat_fallback"},
            )

        stop_distance = atr_14 * self._atr_stop_mult
        risk_per_trade = equity * self._risk_fraction
        raw_qty = risk_per_trade / stop_distance

        qty = max(math.floor(min(raw_qty, budget_qty)), 0)
        if qty <= 0:
            return SizingResult(qty=0, used_atr=True, notes={"reason": "sized_to_zero"})

        return SizingResult(
            qty=qty,
            stop_price=price - stop_distance,
            take_profit_price=price + stop_distance * self._take_profit_mult,
            used_atr=True,
        )

    @staticmethod
    def _max_qty_from_budgets(price: float, budgets: SizingBudgets) -> float:
        return min(
            budgets.max_position_value / price,
            budgets.remaining_exposure_budget / price,
            budgets.remaining_sector_budget / price,
            budgets.buying_power / price,
        )
