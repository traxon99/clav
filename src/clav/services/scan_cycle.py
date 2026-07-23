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

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from clav.clock import Clock
from clav.common.git_sha import UNKNOWN_SHA
from clav.common.logging import bind_cycle_id, clear_cycle_id, get_logger
from clav.config import RiskKnobsOverride, RuntimeOverrides
from clav.data import tables
from clav.data.db import session_scope
from clav.data.repositories import Repositories
from clav.domain.decision import DecisionEngine, Thresholds, Weights
from clav.domain.indicators import IndicatorService
from clav.domain.models import (
    EarningsEvent,
    Fill,
    HealthEvent,
    IndicatorSet,
    MarketClock,
    PortfolioSnapshot,
    Timeframe,
)
from clav.domain.portfolio import PortfolioManager
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import RiskContext, TradingWindow
from clav.domain.risk.sizing import PositionSizer, SizingBudgets, SizingResult
from clav.integrations.llm.client import GeminiRestClient
from clav.interfaces.broker import Broker
from clav.interfaces.market_data import MarketDataSource
from clav.services.analyst_gateway import AnalystGateway
from clav.services.decision_journal import ApprovalPolicy, DecisionJournal
from clav.services.discovery import DiscoveryService
from clav.services.execution import AlertHook, ExecutionEngine
from clav.services.health_monitor import HealthMonitor
from clav.services.runtime_config import RuntimeConfigStore
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
        approval_policy: ApprovalPolicy | None = None,
        runtime_config: RuntimeConfigStore | None = None,
        discovery_service: DiscoveryService | None = None,
        discovery_enabled: bool = False,
        on_demand_enabled: bool = False,
        on_demand_max_per_cycle: int = 5,
        gemini_client: GeminiRestClient | None = None,
        health_monitor: HealthMonitor | None = None,
        config_snapshot_base: dict[str, Any] | None = None,
        git_sha: str = UNKNOWN_SHA,
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
        self._approval_policy = approval_policy
        self._runtime_config = runtime_config
        self._discovery_service = discovery_service
        self._discovery_enabled = discovery_enabled
        self._on_demand_enabled = on_demand_enabled
        self._on_demand_max_per_cycle = on_demand_max_per_cycle
        self._gemini_client = gemini_client
        self._health_monitor = health_monitor
        self._config_snapshot_base = config_snapshot_base or {}
        self._git_sha = git_sha
        # Read by Scheduler after each run() to live-reschedule the scan
        # interval (see services/scheduler.py) without this service needing a
        # reference back to it. None ⇒ no override set this cycle (or no
        # runtime_config wired at all) ⇒ Scheduler leaves its cadence alone.
        self.last_scan_interval_override: int | None = None

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
            if self._health_monitor is not None:
                self._health_monitor.record_startup(repos)

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

    def refresh_asset_universe(self) -> None:
        """Refresh the cached Alpaca tradeable-asset catalog (autonomous-discovery
        epic). Bounded, fail-open, and off the scan path: a failure or an empty
        result leaves the existing catalog untouched, never breaking a cycle."""
        try:
            assets = self._data_source.list_assets()
        except Exception as exc:
            _logger.error("asset_universe_refresh_failed", error=str(exc))
            return
        if not assets:
            _logger.info("asset_universe_refresh_empty")
            return
        with session_scope(self._session_factory) as session:
            repos = Repositories(session)
            written = repos.assets.upsert_many(
                [a.model_dump() for a in assets], updated_at=self._clock.now()
            )
        _logger.info("asset_universe_refreshed", count=written)

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

            watchlist, risk_knobs, runtime_override = self._apply_runtime_overrides(repos)
            self._persist_config_snapshot(repos, cycle_id=cycle_id, override=runtime_override)

            execution = ExecutionEngine(
                self._broker, repos, clock=self._clock, alert_hook=self._alert_hook
            )
            journal = DecisionJournal(
                repos=repos,
                execution=execution,
                clock=self._clock,
                policy=self._approval_policy or ApprovalPolicy(),
            )
            journal.expire_stale()
            # Broker-touching side of Story 3.7/3.8 approval: the web process
            # only ever marks a proposal "approved" (DB-only); clav-core is
            # the only process with broker credentials, so it performs the
            # actual submission here, once per cycle.
            journal.execute_pending_approvals()
            portfolio = PortfolioManager(repos, clock=self._clock, sector_map=self._sector_map)
            portfolio_snapshot = portfolio.reconcile(self._broker)

            daily_start_equity = self._read_daily_start_equity(repos)
            if not emergency_stop:
                emergency_stop = self._check_daily_loss_circuit_breaker(
                    repos, portfolio_snapshot, daily_start_equity, risk_knobs, cycle_id=cycle_id
                )
            post_loss_cooldown_active = self._check_post_loss_cooldown(
                repos, self._clock.now(), risk_knobs
            )

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

            # The effective universe this cycle: operator pins (the watchlist)
            # plus any on-demand "analyze this ticker" requests plus the
            # autonomous-discovery shortlist -- all through the identical
            # _process_symbol path, so the risk gate/e-stop/cooldowns apply to
            # discovered names exactly as they do to pinned ones.
            universe, on_demand = self._build_universe(
                repos,
                watchlist=watchlist,
                override=runtime_override,
            )

            for symbol in universe:
                try:
                    self._process_symbol(
                        cycle_id=cycle_id,
                        symbol=symbol,
                        repos=repos,
                        journal=journal,
                        portfolio=portfolio,
                        portfolio_snapshot=portfolio_snapshot,
                        market_open=market_open,
                        emergency_stop=emergency_stop,
                        paused=paused,
                        daily_start_equity=daily_start_equity,
                        post_loss_cooldown_active=post_loss_cooldown_active,
                        risk_knobs=risk_knobs,
                    )
                except Exception as exc:
                    _logger.error(
                        "scan_cycle_symbol_failed", symbol=symbol, error=str(exc), cycle_id=cycle_id
                    )
                    continue

            self._link_on_demand_results(repos, cycle_id, on_demand)

            self._persist_llm_budget_snapshot(repos)
            self._run_health_monitor(
                repos,
                cycle_id=cycle_id,
                alpaca_ok=market_clock is not None,
                portfolio_snapshot=portfolio_snapshot,
                daily_start_equity=daily_start_equity,
                risk_knobs=risk_knobs,
                emergency_stop=emergency_stop,
                market_open=market_open,
            )
            repos.scan_cycles.finish(cycle_id, finished_at=self._clock.now(), status="completed")
        return cycle_id

    def _run_health_monitor(
        self,
        repos: Repositories,
        *,
        cycle_id: str,
        alpaca_ok: bool,
        portfolio_snapshot: PortfolioSnapshot,
        daily_start_equity: float | None,
        risk_knobs: RiskKnobsOverride,
        emergency_stop: bool,
        market_open: bool,
    ) -> None:
        if self._health_monitor is None:
            return
        try:
            self._health_monitor.run_cycle_end(
                repos,
                cycle_id=cycle_id,
                watchlist=self._watchlist,
                alpaca_ok=alpaca_ok,
                llm_budget_snapshot=(
                    self._analyst_gateway.budget_snapshot()
                    if self._analyst_gateway is not None
                    else None
                ),
                portfolio_snapshot=portfolio_snapshot,
                daily_start_equity=daily_start_equity,
                max_daily_loss_pct=risk_knobs.max_daily_loss_pct,
                max_drawdown_pct=risk_knobs.max_drawdown_pct,
                emergency_stop=emergency_stop,
                market_open=market_open,
            )
        except Exception as exc:
            _logger.error("health_monitor_failed", error=str(exc), cycle_id=cycle_id)

    def _persist_llm_budget_snapshot(self, repos: Repositories) -> None:
        """So the separate ``clav-web`` process (Story 3.8's ``/health``) can
        report Gemini breaker/budget state without reading clav-core's
        in-memory objects directly — see ``AnalystGateway.budget_snapshot``."""
        if self._analyst_gateway is None:
            return
        snapshot = self._analyst_gateway.budget_snapshot()
        if snapshot is None:
            return
        repos.system_control.set(
            "llm_budget_snapshot",
            json.dumps(snapshot),
            updated_at=self._clock.now(),
            updated_by="system:gemini_budget",
        )

    def _apply_runtime_overrides(
        self, repos: Repositories
    ) -> tuple[list[str], RiskKnobsOverride, RuntimeOverrides | None]:
        """Merge the Story-3.8 operator override (if any) on top of boot-time
        config, live-apply weights/thresholds to the decision engine, and
        return the effective watchlist + risk-knob subset for this cycle (plus
        the raw override, for Story 4.4's config_snapshot). No override wired
        ⇒ pure boot-time behavior (regression guard)."""
        if self._runtime_config is None:
            risk_knobs = RiskKnobsOverride(
                max_position_value=self._max_position_value,
                max_daily_loss_pct=self._max_daily_loss_pct,
                max_drawdown_pct=self._max_drawdown_pct,
                max_portfolio_exposure_pct=self._max_portfolio_exposure_pct,
                max_sector_allocation_pct=self._max_sector_allocation_pct,
                cooldown_minutes=self._cooldown_minutes,
                post_loss_cooldown_minutes=self._post_loss_cooldown_minutes,
            )
            return self._watchlist, risk_knobs, None

        override = self._runtime_config.get(repos)
        if override.weights is not None:
            self._decision_engine.update_weights(
                Weights(
                    technical=override.weights.technical,
                    llm=override.weights.llm,
                    portfolio=override.weights.portfolio,
                )
            )
        if override.thresholds is not None:
            self._decision_engine.update_thresholds(
                Thresholds(buy=override.thresholds.buy, sell=override.thresholds.sell)
            )
        if override.llm is not None and self._gemini_client is not None:
            self._gemini_client.reconfigure(
                model=override.llm.model, thinking_budget=override.llm.thinking_budget
            )
        # Scheduler reads this right after run() to live-reschedule its own
        # cadence -- see the attribute's docstring in __init__.
        self.last_scan_interval_override = override.scan_interval_minutes

        risk_knobs = override.risk or RiskKnobsOverride(
            max_position_value=self._max_position_value,
            max_daily_loss_pct=self._max_daily_loss_pct,
            max_drawdown_pct=self._max_drawdown_pct,
            max_portfolio_exposure_pct=self._max_portfolio_exposure_pct,
            max_sector_allocation_pct=self._max_sector_allocation_pct,
            cooldown_minutes=self._cooldown_minutes,
            post_loss_cooldown_minutes=self._post_loss_cooldown_minutes,
        )
        watchlist = override.watchlist or self._watchlist
        return watchlist, risk_knobs, override

    def _open_position_symbols(self, repos: Repositories) -> set[str]:
        symbols: set[str] = set()
        for row in repos.positions.get_all():
            if row.qty == 0:
                continue
            instrument = repos.instruments.get_by_id(row.instrument_id)
            if instrument is not None:
                symbols.add(instrument.symbol.upper())
        return symbols

    def _catalog_rejects(self, repos: Repositories, symbol: str) -> bool:
        """True only when the asset catalog is populated *and* says the symbol
        isn't tradable. An empty (never-refreshed) catalog never vetoes, so
        discovery/on-demand still work before the first asset refresh."""
        return repos.assets.count() > 0 and not repos.assets.is_tradable(symbol)

    def _build_universe(
        self,
        repos: Repositories,
        *,
        watchlist: list[str],
        override: RuntimeOverrides | None,
    ) -> tuple[list[str], list[tuple[int, str]]]:
        """Return (ordered de-duped symbols to scan, drained on-demand
        (request_id, symbol) pairs). Order: pins first, then on-demand, then
        discovered."""
        pins = list(dict.fromkeys(s.upper() for s in watchlist))
        universe = list(pins)
        seen = set(pins)
        on_demand: list[tuple[int, str]] = []

        if self._on_demand_enabled:
            for req in repos.analysis_requests.list_pending(limit=self._on_demand_max_per_cycle):
                symbol = req.symbol.upper()
                if self._catalog_rejects(repos, symbol):
                    repos.analysis_requests.mark_failed(req.id, error="not a tradable symbol")
                    continue
                on_demand.append((req.id, symbol))
                if symbol not in seen:
                    universe.append(symbol)
                    seen.add(symbol)

        discovery_enabled = self._discovery_enabled
        if override is not None and override.discovery_enabled is not None:
            discovery_enabled = override.discovery_enabled
        if discovery_enabled and self._discovery_service is not None:
            try:
                discovered = self._discovery_service.candidates_for_cycle(
                    repos,
                    pins=set(pins),
                    open_symbols=self._open_position_symbols(repos),
                )
            except Exception as exc:  # discovery must never break a scan cycle
                _logger.error("discovery_failed", error=str(exc))
                discovered = []
            for symbol in discovered:
                if symbol not in seen:
                    universe.append(symbol)
                    seen.add(symbol)

        return universe, on_demand

    def _link_on_demand_results(
        self, repos: Repositories, cycle_id: str, on_demand: list[tuple[int, str]]
    ) -> None:
        """Point each drained on-demand request at the decision it produced this
        cycle (if any) and mark it done."""
        if not on_demand:
            return
        cycle_decisions = repos.decisions.list_by_cycle(cycle_id)
        by_instrument: dict[int, int] = {}
        for d in cycle_decisions:
            # keep the latest decision id per instrument
            if d.instrument_id not in by_instrument or d.id > by_instrument[d.instrument_id]:
                by_instrument[d.instrument_id] = d.id
        for req_id, symbol in on_demand:
            instrument = repos.instruments.get_by_symbol(symbol)
            decision_id = by_instrument.get(instrument.id) if instrument is not None else None
            repos.analysis_requests.mark_done(req_id, decision_id=decision_id)

    def _persist_config_snapshot(
        self, repos: Repositories, *, cycle_id: str, override: RuntimeOverrides | None
    ) -> None:
        """Story 4.4: the *effective* (boot config + any live operator
        override) redacted config for this cycle, so any historical decision
        can be explained against the exact settings that produced it
        (docs/10-observability.md §5). Never aborts the cycle.

        The config is round-tripped through ``json.dumps``/``loads`` (with a
        ``str`` fallback for anything odd) *before* it ever reaches the
        session: a raw unserializable value reaching SQLAlchemy's JSON column
        would fail mid-flush and poison the whole cycle's transaction for
        every subsequent write, not just this one — sanitizing here keeps
        that failure mode from ever starting.
        """
        try:
            effective = dict(self._config_snapshot_base)
            if override is not None:
                if override.weights is not None:
                    effective["weights"] = override.weights.model_dump(mode="json")
                if override.thresholds is not None:
                    effective["thresholds"] = override.thresholds.model_dump(mode="json")
                if override.risk is not None:
                    effective["risk"] = {
                        **effective.get("risk", {}),
                        **override.risk.model_dump(mode="json"),
                    }
                if override.watchlist is not None:
                    effective["watchlist"] = override.watchlist
                if override.scan_interval_minutes is not None:
                    effective["scan_interval_minutes"] = override.scan_interval_minutes
                if override.llm is not None:
                    effective["llm"] = override.llm.model_dump(mode="json")
            safe_config = json.loads(json.dumps(effective, default=str))
            repos.config_snapshots.add_for_cycle(
                cycle_id, git_sha=self._git_sha, config=safe_config, created_at=self._clock.now()
            )
        except Exception as exc:
            _logger.error("config_snapshot_failed", error=str(exc), cycle_id=cycle_id)

    def _process_symbol(
        self,
        *,
        cycle_id: str,
        symbol: str,
        repos: Repositories,
        journal: DecisionJournal,
        portfolio: PortfolioManager,
        portfolio_snapshot: PortfolioSnapshot,
        market_open: bool,
        emergency_stop: bool,
        paused: bool,
        daily_start_equity: float | None,
        post_loss_cooldown_active: bool,
        risk_knobs: RiskKnobsOverride,
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
            repos, instrument, self._clock.now(), risk_knobs
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
            sizing = self._size_entry(instrument, iset, portfolio_snapshot, risk_knobs)
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
            max_position_value=risk_knobs.max_position_value,
            buying_power_buffer_pct=self._buying_power_buffer_pct,
            emergency_stop=emergency_stop,
            paused=paused,
            daily_start_equity=daily_start_equity,
            max_daily_loss_pct=risk_knobs.max_daily_loss_pct,
            max_drawdown_pct=risk_knobs.max_drawdown_pct,
            max_portfolio_exposure_pct=risk_knobs.max_portfolio_exposure_pct,
            sector=instrument.sector or "unknown",
            max_sector_allocation_pct=risk_knobs.max_sector_allocation_pct,
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

        rationale = str(llm_provenance.get("rationale", "")) if llm_provenance else ""
        inputs_ref = (
            {
                "news_item_ids": llm_provenance.get("news_item_ids", []),
                "social_digest_id": llm_provenance.get("social_digest_id"),
                "analysis_result_id": llm_provenance.get("analysis_result_id"),
            }
            if llm_provenance
            else {}
        )
        journal_result = journal.record(
            decision=decision,
            decision_id=decision_id,
            risk_decision=risk_decision,
            rationale=rationale,
            inputs_ref=inputs_ref,
        )
        order = journal_result.order
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
            "news_sentiment": signal.news_sentiment,
            "social_sentiment": signal.social_sentiment,
            "conviction": signal.conviction,
            "rationale": signal.rationale,
            "prompt_version": signal.prompt_version,
            "model": signal.model,
            "is_fallback": signal.is_fallback,
            "news_item_ids": result.news_item_ids,
            "social_digest_id": result.social_digest_id,
            "analysis_result_id": result.analysis_result_id,
        }
        return signal.llm_signal, provenance

    def _size_entry(
        self,
        instrument: tables.Instrument,
        iset: IndicatorSet,
        portfolio_snapshot: PortfolioSnapshot,
        risk_knobs: RiskKnobsOverride,
    ) -> SizingResult:
        sector = instrument.sector or "unknown"
        sector_exposure = portfolio_snapshot.sector_allocation.get(sector, 0.0)
        remaining_exposure_budget = max(
            0.0,
            risk_knobs.max_portfolio_exposure_pct * portfolio_snapshot.equity
            - portfolio_snapshot.gross_exposure,
        )
        remaining_sector_budget = max(
            0.0, risk_knobs.max_sector_allocation_pct * portfolio_snapshot.equity - sector_exposure
        )
        budgets = SizingBudgets(
            max_position_value=risk_knobs.max_position_value,
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
        self,
        repos: Repositories,
        instrument: tables.Instrument,
        now: datetime,
        risk_knobs: RiskKnobsOverride,
    ) -> bool:
        """CooldownRule (Story 2.9) per-symbol input: did a trade in this
        symbol *close* within ``cooldown_minutes``? Guards against churning
        an immediate re-entry right after exiting — an already-open position
        is separately excluded by ``DecisionEngine``'s own holding check."""
        last_trade = repos.trades.get_last_closed_trade(instrument.id)
        if last_trade is None or last_trade.closed_at is None:
            return False
        elapsed = (now - _as_utc(last_trade.closed_at)).total_seconds()
        return elapsed < risk_knobs.cooldown_minutes * 60

    def _check_post_loss_cooldown(
        self, repos: Repositories, now: datetime, risk_knobs: RiskKnobsOverride
    ) -> bool:
        """CooldownRule (Story 2.9) global input: did *any* symbol realize a
        loss within ``post_loss_cooldown_minutes``? Freezes every new entry,
        not just the losing symbol's, as a revenge-trade guard."""
        last_loss = repos.trades.get_last_loss()
        if last_loss is None or last_loss.closed_at is None:
            return False
        elapsed = (now - _as_utc(last_loss.closed_at)).total_seconds()
        return elapsed < risk_knobs.post_loss_cooldown_minutes * 60

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
        risk_knobs: RiskKnobsOverride,
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
        if daily_loss_pct < risk_knobs.max_daily_loss_pct:
            return False

        now = self._clock.now()
        repos.system_control.set(
            "emergency_stop", "true", updated_at=now, updated_by="MaxDailyLossRule"
        )
        message = (
            f"daily loss {daily_loss_pct:.2%} breached max_daily_loss_pct "
            f"{risk_knobs.max_daily_loss_pct:.2%}; emergency_stop auto-tripped"
        )
        _logger.critical(
            "daily_loss_auto_estop_tripped",
            cycle_id=cycle_id,
            daily_loss_pct=daily_loss_pct,
            max_daily_loss_pct=risk_knobs.max_daily_loss_pct,
        )
        repos.health_events.add_many(
            [
                HealthEvent(
                    ts=now,
                    category="alert",
                    name="daily_loss_circuit_breaker",
                    status="critical",
                    value={"message": message, "daily_loss_pct": daily_loss_pct},
                    cycle_id=cycle_id,
                )
            ]
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
