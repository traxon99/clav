"""ScanCycleService — wires the whole Epic-1 loop together for one scan cycle:
gate (market open + estop/pause) -> data -> indicators -> decision
(llm_signal=0) -> guardrails -> execution -> portfolio update -> persist
(docs/epics/epic-01-foundation.md, Story 1.13).

One SQLAlchemy session per cycle (unit-of-work pattern): ``ExecutionEngine``
and ``PortfolioManager`` are constructed fresh inside ``run()`` bound to that
session, rather than held long-lived across cycles as the docs/05 composition
-root sample sketches — safer under SQLite/WAL and avoids stale cached ORM
state across a 15-60 minute cadence. See Story 1.12's commit for the same
reasoning applied to PortfolioManager.

The class diagram (docs/05-class-design.md) draws ExecutionEngine ->
PortfolioManager directly; here that hop is instead made by this orchestrator
(call execution, then feed any resulting fill into the portfolio manager) so
Story 1.11's ExecutionEngine doesn't need to know about PortfolioManager at
all — functionally equivalent, and keeps that already-tested engine unchanged.

Per-ticker isolation: one symbol's failure is logged and skipped; it never
aborts the rest of the cycle.

Story 2.3 threads ``PositionSizer`` into this flow for BUY decisions: it
computes an ATR-based stop-loss/take-profit off the live snapshot's exposure
/sector budgets and passes them to ``PortfolioManager.apply_fill`` when the
order actually fills. The sizer's ``qty`` doesn't yet replace the flat
``target_qty`` the Epic-1 ``DecisionEngine``/``RiskEngine`` still drive
order size with — that swap is Story 2.10's job.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session, sessionmaker

from clav.clock import Clock
from clav.common.logging import bind_cycle_id, clear_cycle_id, get_logger
from clav.data import tables
from clav.data.db import session_scope
from clav.data.repositories import Repositories
from clav.domain.decision import DecisionEngine
from clav.domain.indicators import IndicatorService
from clav.domain.models import Fill, IndicatorSet, MarketClock, PortfolioSnapshot, Timeframe
from clav.domain.portfolio import PortfolioManager
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import RiskContext, TradingWindow
from clav.domain.risk.sizing import PositionSizer, SizingBudgets, SizingResult
from clav.interfaces.broker import Broker
from clav.interfaces.market_data import MarketDataSource
from clav.services.execution import AlertHook, ExecutionEngine

_logger = get_logger(__name__)


class ScanCycleService:
    def __init__(
        self,
        *,
        watchlist: list[str],
        data_source: MarketDataSource,
        indicators: IndicatorService,
        decision_engine: DecisionEngine,
        risk_engine: RiskEngine,
        position_sizer: PositionSizer,
        broker: Broker,
        session_factory: sessionmaker[Session],
        clock: Clock,
        trading_window: TradingWindow,
        max_position_value: float,
        buying_power_buffer_pct: float,
        max_portfolio_exposure_pct: float,
        max_sector_allocation_pct: float,
        mode: str,
        candle_timeframe: Timeframe = "1Day",
        candle_limit: int = 200,
        alert_hook: AlertHook | None = None,
    ) -> None:
        self._watchlist = watchlist
        self._data_source = data_source
        self._indicators = indicators
        self._decision_engine = decision_engine
        self._risk_engine = risk_engine
        self._position_sizer = position_sizer
        self._broker = broker
        self._session_factory = session_factory
        self._clock = clock
        self._trading_window = trading_window
        self._max_position_value = max_position_value
        self._buying_power_buffer_pct = buying_power_buffer_pct
        self._max_portfolio_exposure_pct = max_portfolio_exposure_pct
        self._max_sector_allocation_pct = max_sector_allocation_pct
        self._mode = mode
        self._candle_timeframe = candle_timeframe
        self._candle_limit = candle_limit
        self._alert_hook = alert_hook

    def startup_reconcile(self) -> None:
        """Run once before the scheduler starts firing cycles (Story 1.11/1.13
        acceptance criteria): sync any orders left open by a prior crash."""
        with session_scope(self._session_factory) as session:
            repos = Repositories(session)
            execution = ExecutionEngine(
                self._broker, repos, clock=self._clock, alert_hook=self._alert_hook
            )
            execution.reconcile()

    def daily_reset(self) -> None:
        """Rebase peak equity and reset the daily-loss baseline (Story 2.2)
        so ``MaxDrawdownRule``/``MaxDailyLossRule`` (Story 2.5) measure
        against today, not a stale all-time high."""
        with session_scope(self._session_factory) as session:
            repos = Repositories(session)
            portfolio = PortfolioManager(repos, clock=self._clock)
            snap = portfolio.daily_reset(self._broker)
        _logger.info(
            "daily_reset_complete",
            equity=snap.equity,
            peak_equity=snap.peak_equity,
            reconciled=snap.reconciled,
        )

    def run(self, cycle_id: str | None = None, *, trigger: str = "scheduled") -> str:
        cycle_id = cycle_id or str(uuid.uuid4())
        bind_cycle_id(cycle_id)
        try:
            return self._run(cycle_id, trigger)
        finally:
            clear_cycle_id()

    def _run(self, cycle_id: str, trigger: str) -> str:
        started_at = self._clock.now()
        with session_scope(self._session_factory) as session:
            repos = Repositories(session)
            repos.scan_cycles.create(
                cycle_id, started_at=started_at, mode=self._mode, trigger=trigger
            )

            market_clock = self._safe_get_market_clock()
            market_open = market_clock.is_open if market_clock is not None else False
            repos.scan_cycles.set_market_open(cycle_id, market_open)

            if not market_open:
                _logger.info("scan_cycle_skipped_market_closed", cycle_id=cycle_id)
                repos.scan_cycles.finish(
                    cycle_id, finished_at=self._clock.now(), status="skipped_market_closed"
                )
                return cycle_id

            emergency_stop = repos.system_control.get("emergency_stop", "false") == "true"
            paused = repos.system_control.get("paused", "false") == "true"

            execution = ExecutionEngine(
                self._broker, repos, clock=self._clock, alert_hook=self._alert_hook
            )
            portfolio = PortfolioManager(repos, clock=self._clock)
            portfolio_snapshot = portfolio.reconcile(self._broker)

            for symbol in self._watchlist:
                try:
                    self._process_symbol(
                        cycle_id=cycle_id,
                        symbol=symbol,
                        repos=repos,
                        execution=execution,
                        portfolio=portfolio,
                        portfolio_snapshot=portfolio_snapshot,
                        market_open=market_open,
                        emergency_stop=emergency_stop,
                        paused=paused,
                    )
                except Exception as exc:
                    _logger.error(
                        "scan_cycle_symbol_failed", symbol=symbol, error=str(exc), cycle_id=cycle_id
                    )
                    continue

            repos.scan_cycles.finish(cycle_id, finished_at=self._clock.now(), status="completed")
        return cycle_id

    def _process_symbol(
        self,
        *,
        cycle_id: str,
        symbol: str,
        repos: Repositories,
        execution: ExecutionEngine,
        portfolio: PortfolioManager,
        portfolio_snapshot: PortfolioSnapshot,
        market_open: bool,
        emergency_stop: bool,
        paused: bool,
    ) -> None:
        candles = self._data_source.get_candles(symbol, self._candle_timeframe, self._candle_limit)
        if not candles:
            _logger.warning("scan_cycle_no_candles", symbol=symbol, cycle_id=cycle_id)
            return

        instrument = repos.instruments.get_or_create(symbol)
        repos.candles.upsert_many(instrument.id, candles)

        iset = self._indicators.compute(candles)
        repos.indicator_sets.add(instrument.id, iset)

        decision = self._decision_engine.decide(
            cycle_id, iset, llm_signal=0.0, portfolio=portfolio_snapshot
        )
        decision_id = repos.decisions.add(
            scan_cycle_id=cycle_id,
            instrument_id=instrument.id,
            decision=decision,
            created_at=self._clock.now(),
        )

        if decision.action == "HOLD":
            return

        sizing_result: SizingResult | None = None
        if decision.action == "BUY":
            sizing_result = self._size_entry(instrument, iset, portfolio_snapshot)
            _logger.info(
                "position_sizing_computed",
                symbol=symbol,
                qty=sizing_result.qty,
                used_atr=sizing_result.used_atr,
                stop_price=sizing_result.stop_price,
                take_profit_price=sizing_result.take_profit_price,
                notes=sizing_result.notes,
            )

        ctx = RiskContext(
            decision=decision,
            portfolio=portfolio_snapshot,
            price=iset.close,
            now=self._clock.now(),
            market_open=market_open,
            trading_window=self._trading_window,
            max_position_value=self._max_position_value,
            buying_power_buffer_pct=self._buying_power_buffer_pct,
            emergency_stop=emergency_stop,
            paused=paused,
            open_order_symbol_sides=self._open_order_symbol_sides(repos),
        )
        risk_decision = self._risk_engine.evaluate(ctx)
        _logger.info(
            "risk_evaluated",
            symbol=symbol,
            approved=risk_decision.approved,
            adjusted_qty=risk_decision.adjusted_qty,
            blocked_by=risk_decision.blocked_by,
        )

        order = execution.execute(decision, risk_decision, decision_id=decision_id)
        has_fill_details = order is not None and order.filled_qty and order.filled_avg_price
        if order is not None and order.status == "filled" and has_fill_details:
            fill = Fill(
                client_order_id=order.client_order_id,
                qty=order.filled_qty,
                price=order.filled_avg_price,
                filled_at=order.updated_at or self._clock.now(),
            )
            if decision.action == "BUY" and sizing_result is not None:
                portfolio.apply_fill(
                    fill,
                    stop_price=sizing_result.stop_price,
                    take_profit_price=sizing_result.take_profit_price,
                )
            else:
                portfolio.apply_fill(fill)

    def _size_entry(
        self,
        instrument: tables.Instrument,
        iset: IndicatorSet,
        portfolio_snapshot: PortfolioSnapshot,
    ) -> SizingResult:
        """ATR-based stop-loss/take-profit for a prospective entry (Story 2.3).
        Budgets are read live off the current snapshot; sector defaults to
        ``"unknown"`` until Story 2.6 tags instruments."""
        equity = portfolio_snapshot.equity
        sector = instrument.sector or "unknown"
        remaining_exposure_budget = (
            self._max_portfolio_exposure_pct * equity - portfolio_snapshot.gross_exposure
        )
        sector_used = portfolio_snapshot.sector_allocation.get(sector, 0.0)
        remaining_sector_budget = self._max_sector_allocation_pct * equity - sector_used
        buying_power = portfolio_snapshot.buying_power * (1 - self._buying_power_buffer_pct)

        budgets = SizingBudgets(
            max_position_value=self._max_position_value,
            remaining_exposure_budget=remaining_exposure_budget,
            remaining_sector_budget=remaining_sector_budget,
            buying_power=buying_power,
        )
        return self._position_sizer.size(
            equity=equity, price=iset.close, atr_14=iset.atr_14, budgets=budgets
        )

    def _safe_get_market_clock(self) -> MarketClock | None:
        try:
            return self._broker.get_clock()
        except Exception as exc:
            _logger.error("scan_cycle_market_clock_failed", error=str(exc))
            return None

    def _open_order_symbol_sides(self, repos: Repositories) -> frozenset[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        for row in repos.orders.get_open_orders():
            instrument = repos.instruments.get_by_id(row.instrument_id)
            if instrument is not None:
                pairs.add((instrument.symbol, row.side))
        return frozenset(pairs)
