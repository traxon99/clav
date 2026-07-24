"""StopMonitor — checks every open position's stop-loss/take-profit against
the latest quote at the start of each cycle, independent of the LLM/decision
path (Story 2.4, docs/06-safety-and-risk.md §3/§5).

Exits route through the same ``ExecutionEngine`` used by decision-driven
sells (idempotent ``client_order_id``, reconciliation) — no bypass of order
safety — but deliberately **do not** go through the ``RiskEngine``/rule
pipeline at all: the whole point of a dedicated stop-monitor is that it can
exit even when every entry-side rule (emergency_stop, paused, any future
freeze rule) is tripped, so it can't be accidentally caught by a rule that
forgets to stay BUY-only. The one order-safety check it does keep is the
duplicate-order guard (an open sell for the symbol already in flight ⇒
skip), passed in by the caller.

Fail-closed on bad data: a quote fetch failure, or a quote older than
``quote_staleness_seconds`` / flagged ``is_stale``, means **no action** —
never exit on data we don't trust.

Bypassing the rule pipeline does not mean skipping the audit trail: each
triggered exit still persists a ``risk_evaluation`` row (an unconditional
approval, ``notes={"source": ...}``) alongside its ``decision`` row, so
every non-HOLD decision — stop-monitor or risk-engine — is reconstructable
the same way.

``flatten()`` (Story 6.3, epic-06 decision #3) is the emergency-stop sibling
of ``check()``: the caller runs it *instead of* ``check()``, once per cycle,
when ``flatten_on_estop`` is set and the estop is tripped. It force-closes
every open position through the identical exit path — no quote/stop-price
gate, since a market SELL doesn't need one and the estop itself is the
trigger — sharing the same duplicate-order guard that makes a
partially-flattened state safe to re-run.
"""

from __future__ import annotations

from typing import Any

from clav.clock import Clock
from clav.common.logging import get_logger
from clav.data.repositories import Repositories
from clav.domain.models import Fill, PortfolioSnapshot, Position, RiskDecision, TradeDecision
from clav.domain.portfolio import PortfolioManager
from clav.interfaces.market_data import MarketDataSource
from clav.services.execution import ExecutionEngine

_logger = get_logger(__name__)


class StopMonitor:
    def __init__(
        self, data_source: MarketDataSource, *, clock: Clock, quote_staleness_seconds: int
    ) -> None:
        self._data_source = data_source
        self._clock = clock
        self._quote_staleness_seconds = quote_staleness_seconds

    def check(
        self,
        cycle_id: str,
        repos: Repositories,
        execution: ExecutionEngine,
        portfolio: PortfolioManager,
        portfolio_snapshot: PortfolioSnapshot,
        open_order_symbol_sides: frozenset[tuple[str, str]],
    ) -> None:
        for position in portfolio_snapshot.positions:
            if position.qty <= 0:
                continue
            if position.stop_price is None and position.take_profit_price is None:
                continue
            if (position.symbol, "sell") in open_order_symbol_sides:
                _logger.info("stop_monitor_skipped_open_sell_exists", symbol=position.symbol)
                continue
            self._check_position(cycle_id, repos, execution, portfolio, position)

    def flatten(
        self,
        cycle_id: str,
        repos: Repositories,
        execution: ExecutionEngine,
        portfolio: PortfolioManager,
        portfolio_snapshot: PortfolioSnapshot,
        open_order_symbol_sides: frozenset[tuple[str, str]],
    ) -> None:
        for position in portfolio_snapshot.positions:
            if position.qty <= 0:
                continue
            if (position.symbol, "sell") in open_order_symbol_sides:
                _logger.info("flatten_on_estop_skipped_open_sell_exists", symbol=position.symbol)
                continue
            _logger.critical(
                "flatten_on_estop_exit_triggered", symbol=position.symbol, qty=position.qty
            )
            self._submit_forced_exit(
                cycle_id,
                repos,
                execution,
                portfolio,
                position,
                reasoning={"source": "flatten_on_estop"},
                notes={"source": "flatten_on_estop"},
            )

    def _check_position(
        self,
        cycle_id: str,
        repos: Repositories,
        execution: ExecutionEngine,
        portfolio: PortfolioManager,
        position: Position,
    ) -> None:
        try:
            quote = self._data_source.get_quote(position.symbol)
        except Exception as exc:
            _logger.warning("stop_monitor_quote_failed", symbol=position.symbol, error=str(exc))
            return

        age_seconds = (self._clock.now() - quote.ts).total_seconds()
        if quote.is_stale or age_seconds > self._quote_staleness_seconds:
            _logger.warning(
                "stop_monitor_stale_quote_skipped", symbol=position.symbol, age_seconds=age_seconds
            )
            return

        trigger: str | None = None
        if position.stop_price is not None and quote.price <= position.stop_price:
            trigger = "stop_loss"
        elif position.take_profit_price is not None and quote.price >= position.take_profit_price:
            trigger = "take_profit"

        if trigger is None:
            return

        _logger.info(
            "stop_monitor_exit_triggered",
            symbol=position.symbol,
            trigger=trigger,
            qty=position.qty,
            trigger_price=quote.price,
        )
        self._submit_forced_exit(
            cycle_id,
            repos,
            execution,
            portfolio,
            position,
            reasoning={
                "source": "stop_monitor",
                "trigger": trigger,
                "trigger_price": quote.price,
                "stop_price": position.stop_price,
                "take_profit_price": position.take_profit_price,
            },
            notes={"source": "stop_monitor", "trigger": trigger},
        )

    def _submit_forced_exit(
        self,
        cycle_id: str,
        repos: Repositories,
        execution: ExecutionEngine,
        portfolio: PortfolioManager,
        position: Position,
        *,
        reasoning: dict[str, Any],
        notes: dict[str, Any],
    ) -> None:
        instrument = repos.instruments.get_by_symbol(position.symbol)
        if instrument is None:
            return

        decision = TradeDecision(
            cycle_id=cycle_id,
            symbol=position.symbol,
            action="SELL",
            target_qty=position.qty,
            raw_score=0.0,
            technical_score=0.0,
            llm_signal=0.0,
            portfolio_bias=0.0,
            reasoning=reasoning,
        )
        decision_id = repos.decisions.add(
            scan_cycle_id=cycle_id,
            instrument_id=instrument.id,
            decision=decision,
            created_at=self._clock.now(),
        )
        risk_decision = RiskDecision(
            approved=True,
            adjusted_qty=position.qty,
            notes=notes,
        )
        repos.risk_evaluations.add(decision_id, risk_decision, evaluated_at=self._clock.now())

        order = execution.execute(decision, risk_decision, decision_id=decision_id)
        has_fill_details = order is not None and order.filled_qty and order.filled_avg_price
        if order is not None and order.status == "filled" and has_fill_details:
            fill = Fill(
                client_order_id=order.client_order_id,
                qty=order.filled_qty,
                price=order.filled_avg_price,
                filled_at=order.updated_at or self._clock.now(),
            )
            portfolio.apply_fill(fill)
