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

Story 2.10: every decision that reaches the risk engine — including a HOLD
(a genuine no-signal, or a candidate BUY the sizer shrank to zero) — persists
a ``risk_evaluation`` row (``RiskEngine.evaluate()`` already returns a fixed
"no actionable decision" result for HOLD, so this is just no longer
short-circuited before reaching it). A closed trade can always be walked back
to the exact rule outcomes that allowed it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from clav.clock import Clock
from clav.common.logging import bind_cycle_id, clear_cycle_id, get_logger
from clav.data import tables
from clav.data.db import session_scope
from clav.data.repositories import Repositories
from clav.domain.decision import DecisionEngine
from clav.domain.indicators import IndicatorService
from clav.domain.models import (
    EarningsEvent,
    Fill,
    IndicatorSet,
    MarketClock,
    PortfolioSnapshot,
    Timeframe,
)
from clav.domain.portfolio import PortfolioManager
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import RiskContext, TradingWindow
from clav.domain.risk.sizing import PositionSizer, SizingBudgets, SizingResult
from clav.interfaces.broker import Broker
from clav.interfaces.market_data import MarketDataSource
from clav.services.analyst_gateway import AnalystGateway
from clav.services.execution import AlertHook, ExecutionEngine
from clav.services.stop_monitor import StopMonitor

_logger = get_logger(__name__)


def _as_utc(dt: datetime) -> datetime:
    """SQLite's DATETIME columns don't round-trip timezone info — a value
    read back via SQLAlchemy comes back naive even though every datetime
    this system writes is UTC (the ``Clock`` contract). Re-tag it rather
    than compare naive against the injected Clock's aware ``now()``."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


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
        stop_monitor: StopMonitor,
        broker: Broker,
        session_factory: sessionmaker[Session],
        clock: Clock,
        trading_window: TradingWindow,
        max_position_value: float,
        buying_power_buffer_pct: float,
        max_portfolio_exposure_pct: float,
        max_sector_allocation_pct: float,
        max_daily_loss_pct: float,
        max_drawdown_pct: float,
        min_avg_volume: float,
        earnings_blackout_days: int,
        cooldown_minutes: int,
        post_loss_cooldown_minutes: int,
        mode: str,
        candle_timeframe: Timeframe = "1Day",
        candle_limit: int = 200,
        alert_hook: AlertHook | None = None,
        sector_map: dict[str, str] | None = None,
        earnings_calendar: list[EarningsEvent] | None = None,
        analyst_gateway: AnalystGateway | None = None,
    ) -> None:
        self._watchlist = watchlist
        self._data_source = data_source
        self._indicators = indicators
        self._decision_engine = decision_engine
        self._risk_engine = risk_engine
        self._position_sizer = position_sizer
        self._stop_monitor = stop_monitor
        self._broker = broker
        self._session_factory = session_factory
        self._clock = clock
        self._trading_window = trading_window
        self._max_position_value = max_position_value
        self._buying_power_buffer_pct = buying_power_buffer_pct
        self._max_portfolio_exposure_pct = max_portfolio_exposure_pct
        self._max_sector_allocation_pct = max_sector_allocation_pct
        self._max_daily_loss_pct = max_daily_loss_pct
        self._max_drawdown_pct = max_drawdown_pct
        self._min_avg_volume = min_avg_volume
        self._earnings_blackout_days = earnings_blackout_days
        self._cooldown_minutes = cooldown_minutes
        self._post_loss_cooldown_minutes = post_loss_cooldown_minutes
        self._mode = mode
        self._candle_timeframe = candle_timeframe
        self._candle_limit = candle_limit
        self._alert_hook = alert_hook
        self._sector_map = sector_map or {}
        self._earnings_calendar = earnings_calendar or []
        self._analyst_gateway = analyst_gateway

    def startup_reconcile(self) -> None:
        """Run once before the scheduler starts firing cycles (Story 1.11/1.13
        acceptance criteria): sync any orders left open by a prior crash, and
        seed the earnings calendar (Story 2.8)."""
        with session_scope(self._session_factory) as session:
            repos = Repositories(session)
            execution = ExecutionEngine(
                self._broker, repos, clock=self._clock, alert_hook=self._alert_hook
            )
            execution.reconcile()
            self._seed_earnings_calendar(repos)

    def _seed_earnings_calendar(self, repos: Repositories) -> None:
        """Minimal earnings source (Story 2.8): a static, config-provided
        calendar seeded into the ``earnings_event`` table once at startup.
        Idempotent (checks before inserting) so repeated restarts don't pile
        up duplicate rows. Full news/EDGAR-driven ingestion is Epic 3."""
        for event in self._earnings_calendar:
            instrument = repos.instruments.get_or_create(
                event.symbol, sector=self._sector_map.get(event.symbol)
            )
            if not repos.earnings_events.exists(
                instrument.id, scheduled_at=event.scheduled_at, event_type=event.event_type
            ):
                repos.earnings_events.add(instrument.id, event)

    def daily_reset(self) -> None:
        """Rebase peak equity and reset the daily-loss baseline (Story 2.2)
        so ``MaxDrawdownRule``/``MaxDailyLossRule`` (Story 2.5) measure
        against today, not a stale all-time high."""
        with session_scope(self._session_factory) as session:
            repos = Repositories(session)
            portfolio = PortfolioManager(repos, clock=self._clock, sector_map=self._sector_map)
            snap = portfolio.daily_reset(self._broker)
        if self._analyst_gateway is not None:
            # Reset the Gemini daily token/cost counters (Story 3.5) on the same
            # existing job rather than adding a new scheduler.
            self._analyst_gateway.reset_daily()
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
            portfolio = PortfolioManager(repos, clock=self._clock, sector_map=self._sector_map)
            portfolio_snapshot = portfolio.reconcile(self._broker)

            daily_start_equity = self._read_daily_start_equity(repos)
            if not emergency_stop:
                emergency_stop = self._check_daily_loss_circuit_breaker(
                    repos, portfolio_snapshot, daily_start_equity, cycle_id=cycle_id
                )
            post_loss_cooldown_active = self._check_post_loss_cooldown(repos, self._clock.now())

            try:
                self._stop_monitor.check(
                    cycle_id,
                    repos,
                    execution,
                    portfolio,
                    portfolio_snapshot,
                    self._open_order_symbol_sides(repos),
                )
            except Exception as exc:
                _logger.error("stop_monitor_failed", error=str(exc), cycle_id=cycle_id)

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
                        daily_start_equity=daily_start_equity,
                        post_loss_cooldown_active=post_loss_cooldown_active,
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
        daily_start_equity: float | None,
        post_loss_cooldown_active: bool,
    ) -> None:
        candles = self._data_source.get_candles(symbol, self._candle_timeframe, self._candle_limit)
        if not candles:
            _logger.warning("scan_cycle_no_candles", symbol=symbol, cycle_id=cycle_id)
            return

        instrument = repos.instruments.get_or_create(symbol, sector=self._sector_map.get(symbol))
        repos.candles.upsert_many(instrument.id, candles)

        iset = self._indicators.compute(candles)
        repos.indicator_sets.add(instrument.id, iset)

        # A wall-clock age check against the candle's own bar timestamp isn't
        # meaningful here: the default 1Day timeframe means the latest closed
        # bar is legitimately ~1 day old by construction. The signal that
        # actually means "we're not looking at live data" is the adapter's
        # own is_stale flag, set when a fetch failure forces a fallback to a
        # cached candle set (see AlpacaDataAdapter.get_candles).
        data_stale = candles[-1].is_stale
        earnings_blackout = self._check_earnings_blackout(repos, instrument, self._clock.now())
        cooldown_active = post_loss_cooldown_active or self._check_symbol_cooldown(
            repos, instrument, self._clock.now()
        )

        llm_signal, llm_provenance = self._analyst_signal(symbol, repos, instrument, iset)
        decision = self._decision_engine.decide(
            cycle_id, iset, llm_signal=llm_signal, portfolio=portfolio_snapshot
        )
        if llm_provenance is not None:
            decision = decision.model_copy(
                update={"reasoning": {**decision.reasoning, "llm": llm_provenance}}
            )

        sizing: SizingResult | None = None
        if decision.action == "BUY":
            sizing = self._size_entry(instrument, iset, portfolio_snapshot)
            sizing_notes = sizing.notes | {"sized_by": sizing.sized_by}
            decision = decision.model_copy(
                update={
                    "action": "BUY" if sizing.qty > 0 else "HOLD",
                    "target_qty": sizing.qty,
                    "reasoning": {**decision.reasoning, "sizing": sizing_notes},
                }
            )

        decision_id = repos.decisions.add(
            scan_cycle_id=cycle_id,
            instrument_id=instrument.id,
            decision=decision,
            created_at=self._clock.now(),
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
            daily_start_equity=daily_start_equity,
            max_daily_loss_pct=self._max_daily_loss_pct,
            max_drawdown_pct=self._max_drawdown_pct,
            max_portfolio_exposure_pct=self._max_portfolio_exposure_pct,
            sector=instrument.sector or "unknown",
            max_sector_allocation_pct=self._max_sector_allocation_pct,
            data_stale=data_stale,
            avg_volume=iset.vol_avg_20,
            min_avg_volume=self._min_avg_volume,
            earnings_blackout=earnings_blackout,
            cooldown_active=cooldown_active,
            open_order_symbol_sides=self._open_order_symbol_sides(repos),
        )
        risk_decision = self._risk_engine.evaluate(ctx)
        repos.risk_evaluations.add(decision_id, risk_decision, evaluated_at=self._clock.now())
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
            stop_price = sizing.stop_price if sizing is not None else None
            take_profit_price = sizing.take_profit_price if sizing is not None else None
            portfolio.apply_fill(fill, stop_price=stop_price, take_profit_price=take_profit_price)

    def _analyst_signal(
        self,
        symbol: str,
        repos: Repositories,
        instrument: tables.Instrument,
        iset: IndicatorSet,
    ) -> tuple[float, dict[str, object] | None]:
        """Produce the advisory ``llm_signal`` behind the risk gate (Story 3.6).

        With no analyst wired in, returns ``(0.0, None)`` — byte-for-byte the
        Epic-2 technical-only path. Any unexpected failure in the gateway also
        degrades to ``0.0`` (chaos hook): the LLM can never abort or hijack a
        cycle, only *nudge* a score that the risk engine still gates."""
        if self._analyst_gateway is None:
            return 0.0, None
        try:
            result = self._analyst_gateway.signal_for(
                symbol,
                repos,
                instrument.id,
                context={
                    "technical_score": iset.technical_score,
                    "close": iset.close,
                    "rsi_14": iset.rsi_14,
                },
                is_low_liquidity=(
                    iset.vol_avg_20 is not None and iset.vol_avg_20 < self._min_avg_volume
                ),
            )
        except Exception as exc:
            _logger.error(
                "analyst_gateway_failed_degrading_to_technical",
                symbol=symbol,
                error=str(exc),
            )
            return 0.0, None

        signal = result.signal
        provenance: dict[str, object] = {
            "sentiment": signal.sentiment,
            "conviction": signal.conviction,
            "rationale": signal.rationale,
            "prompt_version": signal.prompt_version,
            "model": signal.model,
            "is_fallback": signal.is_fallback,
            "news_item_ids": result.news_item_ids,
            "social_digest_id": result.social_digest_id,
        }
        return signal.llm_signal, provenance

    def _size_entry(
        self,
        instrument: tables.Instrument,
        iset: IndicatorSet,
        portfolio_snapshot: PortfolioSnapshot,
    ) -> SizingResult:
        sector = instrument.sector or "unknown"
        sector_exposure = portfolio_snapshot.sector_allocation.get(sector, 0.0)
        remaining_exposure_budget = max(
            0.0,
            self._max_portfolio_exposure_pct * portfolio_snapshot.equity
            - portfolio_snapshot.gross_exposure,
        )
        remaining_sector_budget = max(
            0.0, self._max_sector_allocation_pct * portfolio_snapshot.equity - sector_exposure
        )
        budgets = SizingBudgets(
            max_position_value=self._max_position_value,
            remaining_exposure_budget=remaining_exposure_budget,
            remaining_sector_budget=remaining_sector_budget,
            buying_power=portfolio_snapshot.buying_power * (1 - self._buying_power_buffer_pct),
        )
        return self._position_sizer.size(
            equity=portfolio_snapshot.equity,
            price=iset.close,
            atr_14=iset.atr_14,
            budgets=budgets,
        )

    def _check_earnings_blackout(
        self, repos: Repositories, instrument: tables.Instrument, now: datetime
    ) -> bool:
        """EarningsBlackoutRule (Story 2.8) input: does the calendar (seeded
        at startup — see ``_seed_earnings_calendar``) know of an earnings
        event within ``earnings_blackout_days`` of now? A symbol with no
        earnings data at all fails *open* (no known blackout) — a deliberate
        choice pending the Epic-3 news/EDGAR-driven feed — and that choice is
        logged here rather than in the (pure, DB-free) rule."""
        upcoming = repos.earnings_events.get_upcoming(instrument.id, after=now)
        if not upcoming:
            _logger.info("earnings_blackout_no_data_fail_open", symbol=instrument.symbol)
            return False
        cutoff = now + timedelta(days=self._earnings_blackout_days)
        return any(_as_utc(event.scheduled_at) <= cutoff for event in upcoming)

    def _check_symbol_cooldown(
        self, repos: Repositories, instrument: tables.Instrument, now: datetime
    ) -> bool:
        """CooldownRule (Story 2.9) per-symbol input: did a trade in this
        symbol *close* within ``cooldown_minutes``? Guards against churning
        an immediate re-entry right after exiting — an already-open position
        is separately excluded by ``DecisionEngine``'s own holding check."""
        last_trade = repos.trades.get_last_closed_trade(instrument.id)
        if last_trade is None or last_trade.closed_at is None:
            return False
        elapsed = (now - _as_utc(last_trade.closed_at)).total_seconds()
        return elapsed < self._cooldown_minutes * 60

    def _check_post_loss_cooldown(self, repos: Repositories, now: datetime) -> bool:
        """CooldownRule (Story 2.9) global input: did *any* symbol realize a
        loss within ``post_loss_cooldown_minutes``? Freezes every new entry,
        not just the losing symbol's, as a revenge-trade guard."""
        last_loss = repos.trades.get_last_loss()
        if last_loss is None or last_loss.closed_at is None:
            return False
        elapsed = (now - _as_utc(last_loss.closed_at)).total_seconds()
        return elapsed < self._post_loss_cooldown_minutes * 60

    def _read_daily_start_equity(self, repos: Repositories) -> float | None:
        raw = repos.system_control.get("daily_start_equity")
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def _check_daily_loss_circuit_breaker(
        self,
        repos: Repositories,
        portfolio_snapshot: PortfolioSnapshot,
        daily_start_equity: float | None,
        *,
        cycle_id: str,
    ) -> bool:
        """MaxDailyLossRule (Story 2.5) vetoes new BUYs on its own, but a
        breach should also durably trip the global ``emergency_stop`` —
        that's a persistence-layer side effect the (pure, DB-free) rule
        can't do itself, so it happens once per cycle here instead."""
        if daily_start_equity is None or daily_start_equity <= 0:
            return False
        daily_loss_pct = (daily_start_equity - portfolio_snapshot.equity) / daily_start_equity
        if daily_loss_pct < self._max_daily_loss_pct:
            return False

        now = self._clock.now()
        repos.system_control.set(
            "emergency_stop", "true", updated_at=now, updated_by="MaxDailyLossRule"
        )
        message = (
            f"daily loss {daily_loss_pct:.2%} breached max_daily_loss_pct "
            f"{self._max_daily_loss_pct:.2%}; emergency_stop auto-tripped"
        )
        _logger.critical(
            "daily_loss_auto_estop_tripped",
            cycle_id=cycle_id,
            daily_loss_pct=daily_loss_pct,
            max_daily_loss_pct=self._max_daily_loss_pct,
        )
        if self._alert_hook is not None:
            self._alert_hook("daily_loss_circuit_breaker", message)
        else:
            _logger.critical("daily_loss_alert_no_hook_configured", message=message)
        return True

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
