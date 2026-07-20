"""PositionSizer — volatility-aware (ATR) position sizing at entry (Story 2.3,
docs/06-safety-and-risk.md §3).

A pure, config-free function of its arguments: no clock reads, no repository
access, no vendor imports. It runs "inside" the risk stage (docs/02-modules.md
§5) but is deliberately its own class (Strategy pattern, docs/05-class-design.md
§2) so it can be table-tested in isolation from the rest of the rule pipeline.

Formula (docs/06-safety-and-risk.md §3):
    risk_per_trade = equity * risk_fraction
    stop_distance  = atr_14 * atr_stop_mult
    raw_qty        = risk_per_trade / stop_distance
    qty = min(raw_qty, max_position_value/price, remaining_exposure_budget/price,
              remaining_sector_budget/price, buying_power/price)

When ``atr_14`` is unavailable (insufficient candle history — see
``IndicatorSet``), sizing fails closed to the flat ``default_order_value/price``
notional used in Epic 1, with no stop-loss/take-profit computed (there is no
ATR to derive them from). The budget clamps still apply to the fallback so it
can never exceed the same per-name/exposure/sector/buying-power caps as
ATR-based sizing — only the *risk-per-trade* raw_qty step is skipped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class SizingBudgets:
    """The non-ATR-dependent caps a sized quantity must fit within. Sourced
    from config (``max_position_value``) and the current ``PortfolioSnapshot``
    (the exposure/sector/buying-power budgets remaining before those caps are
    hit) — see ``ScanCycleService._process_symbol``."""

    max_position_value: float
    remaining_exposure_budget: float
    remaining_sector_budget: float
    buying_power: float


@dataclass(frozen=True)
class SizingResult:
    qty: int
    stop_price: float | None
    take_profit_price: float | None
    sized_by: Literal["atr", "flat", "none"]
    notes: dict[str, Any] = field(default_factory=dict)


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
            return SizingResult(
                qty=0,
                stop_price=None,
                take_profit_price=None,
                sized_by="none",
                notes={"reason": "invalid price"},
            )

        if atr_14 is None or atr_14 <= 0:
            raw_qty = self._default_order_value / price
            qty = math.floor(self._clamp(raw_qty, price, budgets))
            return SizingResult(
                qty=max(qty, 0),
                stop_price=None,
                take_profit_price=None,
                sized_by="flat",
                notes={"reason": "atr_14 unavailable; flat default_order_value fallback"},
            )

        risk_per_trade = equity * self._risk_fraction
        stop_distance = atr_14 * self._atr_stop_mult
        raw_qty = risk_per_trade / stop_distance
        qty = math.floor(self._clamp(raw_qty, price, budgets))

        if qty <= 0:
            return SizingResult(
                qty=0,
                stop_price=None,
                take_profit_price=None,
                sized_by="atr",
                notes={"reason": "sized to zero after budget clamps", "raw_qty": raw_qty},
            )

        return SizingResult(
            qty=qty,
            stop_price=price - stop_distance,
            take_profit_price=price + stop_distance * self._take_profit_mult,
            sized_by="atr",
            notes={"raw_qty": raw_qty, "stop_distance": stop_distance},
        )

    @staticmethod
    def _clamp(raw_qty: float, price: float, budgets: SizingBudgets) -> float:
        caps = [
            raw_qty,
            budgets.max_position_value / price,
            budgets.remaining_exposure_budget / price,
            budgets.remaining_sector_budget / price,
            budgets.buying_power / price,
        ]
        return max(0.0, min(caps))
