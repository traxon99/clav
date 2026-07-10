"""DecisionEngine — combines technical score, advisory LLM signal, and portfolio
bias into a candidate Buy/Sell/Hold action (docs/00-overview.md §4,
docs/02-modules.md §4). It proposes; it never executes.

In Epic 1, ``llm_signal`` is always 0 (no Analyst wired in yet) and sizing is a
flat USD notional per entry — the full ATR-based PositionSizer arrives in
Epic 2. Weights/Thresholds are plain domain value types (not clav.config's
pydantic-settings models) so this module stays free of any config/env/vendor
machinery; the composition root translates config into these at startup.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from clav.clock import Clock
from clav.domain.models import Action, IndicatorSet, PortfolioSnapshot, TradeDecision


@dataclass(frozen=True)
class Weights:
    technical: float
    llm: float
    portfolio: float


@dataclass(frozen=True)
class Thresholds:
    buy: float
    sell: float


def _clip(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


class DecisionEngine:
    def __init__(
        self,
        weights: Weights,
        thresholds: Thresholds,
        *,
        default_order_value: float,
        clock: Clock,
    ) -> None:
        self._weights = weights
        self._thresholds = thresholds
        self._default_order_value = default_order_value
        self._clock = clock

    def decide(
        self,
        cycle_id: str,
        iset: IndicatorSet,
        llm_signal: float,
        portfolio: PortfolioSnapshot,
    ) -> TradeDecision:
        technical_score = iset.technical_score if iset.technical_score is not None else 0.0
        llm_signal = _clip(llm_signal)
        current_qty = next(
            (p.qty for p in portfolio.positions if p.symbol == iset.symbol), 0
        )
        holding = current_qty > 0
        portfolio_bias = self._portfolio_bias(iset.symbol, portfolio)

        raw_score = (
            self._weights.technical * technical_score
            + self._weights.llm * llm_signal
            + self._weights.portfolio * portfolio_bias
        )

        action: Action
        if raw_score > self._thresholds.buy and not holding:
            action = "BUY"
        elif raw_score < self._thresholds.sell and holding:
            action = "SELL"
        else:
            action = "HOLD"

        target_qty = self._target_qty(action, iset.close, current_qty)
        if action == "BUY" and target_qty == 0:
            # Price too high for the default notional to buy even one share —
            # fail closed to HOLD rather than submit a zero-quantity order.
            action = "HOLD"

        reasoning: dict[str, Any] = {
            "technical_score": technical_score,
            "llm_signal": llm_signal,
            "portfolio_bias": portfolio_bias,
            "raw_score": raw_score,
            "weights": {
                "technical": self._weights.technical,
                "llm": self._weights.llm,
                "portfolio": self._weights.portfolio,
            },
            "thresholds": {"buy": self._thresholds.buy, "sell": self._thresholds.sell},
            "holding": holding,
            "current_qty": current_qty,
            "evaluated_at": self._clock.now().isoformat(),
        }

        return TradeDecision(
            cycle_id=cycle_id,
            symbol=iset.symbol,
            action=action,
            target_qty=target_qty,
            raw_score=raw_score,
            technical_score=technical_score,
            llm_signal=llm_signal,
            portfolio_bias=portfolio_bias,
            reasoning=reasoning,
        )

    def _portfolio_bias(self, symbol: str, portfolio: PortfolioSnapshot) -> float:
        # Stubbed at 0.0 in Epic 1 (weights.portfolio defaults to 0.0 too — see
        # config.example.yaml). Sector/exposure-aware bias lands in Epic 2/3.
        return 0.0

    def _target_qty(self, action: Action, price: float, current_qty: int) -> int:
        if action == "BUY":
            if price <= 0:
                return 0
            return math.floor(self._default_order_value / price)
        if action == "SELL":
            return current_qty  # full exit; partial-close sizing is Epic 2
        return 0
