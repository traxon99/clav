"""Story 2.11 — the six Epic-2 risk invariants CI must enforce
(docs/epics/epic-02-risk-and-portfolio.md, Story 2.11). Each is proven
directly here for auditability, even though several are also exercised
incidentally elsewhere (test_risk_engine.py's per-rule hypothesis properties,
test_scan_cycle.py's end-to-end cycles) — a safety-critical invariant
deserves one file a reviewer can open and see all six proven, rather than
being inferred from scattered component tests. Mirrors the rationale of
Epic 1's test_safety_invariants.py.

1. No rule ever increases qty (monotone-shrink).
2. Any veto => approved=False, adjusted_qty=0.
3. emergency_stop / paused => no BUY is ever approved.
4. Every freeze/cap rule leaves SELL/exit decisions untouched.
5. A MaxDailyLossRule breach auto-trips emergency_stop.
6. A risk_evaluation row exists for every non-HOLD decision.

Invariants 1-4 are pure RiskEngine properties, proven with hypothesis over a
single, fully-randomized RiskContext strategy that varies every rule's inputs
at once — broader than the per-rule property tests added story-by-story,
which mostly vary one dimension at a time. Invariants 5-6 are service-level
side effects (the auto-estop trip and risk_evaluation persistence both live
in ScanCycleService, not the pure rule engine — see Stories 2.5/2.10), so
they're proven by driving a real cycle end to end.
"""

from __future__ import annotations

from datetime import time
from unittest.mock import MagicMock

import pytest
from conftest import NOON_UTC, FakeMarketDataSource
from conftest import trending_candles as _trending_candles
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select

from clav.clock import FakeClock
from clav.data import tables
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.decision import DecisionEngine, Thresholds, Weights
from clav.domain.indicators import IndicatorService
from clav.domain.models import Account, MarketClock, PortfolioSnapshot, TradeDecision
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import RiskContext, TradingWindow, default_rules
from clav.domain.risk.sizing import PositionSizer
from clav.integrations.dryrun_broker import DryRunBroker
from clav.interfaces.broker import Broker
from clav.services.scan_cycle import ScanCycleService
from clav.services.stop_monitor import StopMonitor

WINDOW = TradingWindow(start=time(9, 35), end=time(15, 55), timezone="America/New_York")


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


# --- shared randomized RiskContext strategy (invariants 1-4) ---------------


@st.composite
def _risk_contexts(
    draw,
    action: str,
    *,
    emergency_stop: bool | None = None,
    paused: bool | None = None,
    open_order_symbol_sides: frozenset[tuple[str, str]] | None = None,
) -> RiskContext:
    """Randomizes every RiskContext input at once (all 15 rules' inputs
    simultaneously), unlike the per-rule property tests in test_risk_engine.py
    which mostly hold everything but one dimension at an "inert" default."""
    price = draw(
        st.floats(min_value=0.01, max_value=10_000.0, allow_nan=False, allow_infinity=False)
    )
    target_qty = draw(st.integers(min_value=1, max_value=10_000))
    equity = draw(
        st.floats(min_value=0.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False)
    )
    buying_power = draw(
        st.floats(min_value=0.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False)
    )
    drawdown = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False))
    gross_exposure = draw(
        st.floats(min_value=0.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False)
    )
    sector_exposure = draw(
        st.floats(min_value=0.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False)
    )
    reconciled = draw(st.booleans())
    max_position_value = draw(
        st.floats(min_value=1.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False)
    )
    buying_power_buffer_pct = draw(
        st.floats(min_value=0.0, max_value=0.99, allow_nan=False, allow_infinity=False)
    )
    es = draw(st.booleans()) if emergency_stop is None else emergency_stop
    pa = draw(st.booleans()) if paused is None else paused
    daily_start_equity = draw(
        st.none()
        | st.floats(min_value=1.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False)
    )
    max_daily_loss_pct = draw(
        st.floats(min_value=0.001, max_value=1.0, allow_nan=False, allow_infinity=False)
    )
    max_drawdown_pct = draw(
        st.floats(min_value=0.001, max_value=1.0, allow_nan=False, allow_infinity=False)
    )
    max_portfolio_exposure_pct = draw(
        st.floats(min_value=0.001, max_value=1.0, allow_nan=False, allow_infinity=False)
    )
    max_sector_allocation_pct = draw(
        st.floats(min_value=0.001, max_value=1.0, allow_nan=False, allow_infinity=False)
    )
    data_stale = draw(st.booleans())
    avg_volume = draw(
        st.none()
        | st.floats(min_value=0.0, max_value=10_000_000.0, allow_nan=False, allow_infinity=False)
    )
    min_avg_volume = draw(
        st.floats(min_value=0.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False)
    )
    earnings_blackout = draw(st.booleans())
    cooldown_active = draw(st.booleans())
    market_open = draw(st.booleans())

    if open_order_symbol_sides is not None:
        sides = open_order_symbol_sides
    else:
        side = "buy" if action == "BUY" else "sell"
        sides = frozenset({("AAPL", side)}) if draw(st.booleans()) else frozenset()

    decision = TradeDecision(
        cycle_id="cycle-1",
        symbol="AAPL",
        action=action,  # type: ignore[arg-type]
        target_qty=target_qty,
        raw_score=0.0,
        technical_score=0.0,
        llm_signal=0.0,
        portfolio_bias=0.0,
    )
    portfolio = PortfolioSnapshot(
        ts=NOON_UTC,
        cash=buying_power,
        equity=equity,
        buying_power=buying_power,
        drawdown=drawdown,
        gross_exposure=gross_exposure,
        sector_allocation={"Technology": sector_exposure},
        reconciled=reconciled,
    )
    return RiskContext(
        decision=decision,
        portfolio=portfolio,
        price=price,
        now=NOON_UTC,
        market_open=market_open,
        trading_window=WINDOW,
        max_position_value=max_position_value,
        buying_power_buffer_pct=buying_power_buffer_pct,
        emergency_stop=es,
        paused=pa,
        daily_start_equity=daily_start_equity,
        max_daily_loss_pct=max_daily_loss_pct,
        max_drawdown_pct=max_drawdown_pct,
        max_portfolio_exposure_pct=max_portfolio_exposure_pct,
        sector="Technology",
        max_sector_allocation_pct=max_sector_allocation_pct,
        data_stale=data_stale,
        avg_volume=avg_volume,
        min_avg_volume=min_avg_volume,
        earnings_blackout=earnings_blackout,
        cooldown_active=cooldown_active,
        open_order_symbol_sides=sides,
    )


# --- Invariant 1: no rule ever increases qty --------------------------------


@given(ctx=_risk_contexts("BUY"))
def test_invariant_1_no_rule_ever_increases_qty(ctx: RiskContext) -> None:
    result = RiskEngine(default_rules()).evaluate(ctx)
    assert result.adjusted_qty <= ctx.decision.target_qty


# --- Invariant 2: any veto => approved=False, adjusted_qty=0 ---------------


@given(ctx=_risk_contexts("BUY"))
def test_invariant_2_any_veto_means_not_approved_and_zero_qty(ctx: RiskContext) -> None:
    result = RiskEngine(default_rules()).evaluate(ctx)
    if result.blocked_by:
        assert result.approved is False
        assert result.adjusted_qty == 0


# --- Invariant 3: emergency_stop/paused => no BUY ever approved ------------


@given(ctx=_risk_contexts("BUY", emergency_stop=True))
def test_invariant_3_emergency_stop_blocks_every_buy(ctx: RiskContext) -> None:
    result = RiskEngine(default_rules()).evaluate(ctx)
    assert result.approved is False


@given(ctx=_risk_contexts("BUY", paused=True))
def test_invariant_3_paused_blocks_every_buy(ctx: RiskContext) -> None:
    result = RiskEngine(default_rules()).evaluate(ctx)
    assert result.approved is False


# --- Invariant 4: every freeze/cap rule leaves SELL/exit untouched ---------


@given(ctx=_risk_contexts("SELL", open_order_symbol_sides=frozenset()))
def test_invariant_4_every_freeze_and_cap_rule_leaves_sell_untouched(ctx: RiskContext) -> None:
    # open_order_symbol_sides forced empty: DuplicateOrderRule is an
    # idempotency guard, not a freeze/cap rule (docs/risk/rules.py), and
    # intentionally applies to both sides -- excluded here to cleanly isolate
    # the freeze/cap invariant this test is about.
    result = RiskEngine(default_rules()).evaluate(ctx)
    assert result.approved is True
    assert result.adjusted_qty == ctx.decision.target_qty


# --- Invariant 5: MaxDailyLossRule breach auto-trips emergency_stop -------


def _broker(*, equity: float) -> MagicMock:
    broker = MagicMock(spec=Broker)
    broker.get_clock.return_value = MarketClock(
        timestamp=NOON_UTC, is_open=True, next_open=NOON_UTC, next_close=NOON_UTC
    )
    broker.get_account.return_value = Account(
        cash=equity, buying_power=equity, equity=equity, portfolio_value=equity
    )
    broker.get_positions.return_value = []
    return broker


def _service(session_factory, data_source, broker, clock) -> ScanCycleService:
    return ScanCycleService(
        watchlist=[],
        data_source=data_source,
        indicators=IndicatorService(),
        decision_engine=DecisionEngine(
            Weights(technical=1.0, llm=0.0, portfolio=0.0),
            Thresholds(buy=0.2, sell=-0.2),
            default_order_value=1000.0,
            clock=clock,
        ),
        risk_engine=RiskEngine(default_rules()),
        position_sizer=PositionSizer(
            risk_fraction=0.01, atr_stop_mult=2.0, take_profit_mult=2.0, default_order_value=1000.0
        ),
        stop_monitor=StopMonitor(data_source, clock=clock, quote_staleness_seconds=300),
        broker=broker,
        session_factory=session_factory,
        clock=clock,
        trading_window=WINDOW,
        max_position_value=2000.0,
        buying_power_buffer_pct=0.05,
        max_portfolio_exposure_pct=0.80,
        max_sector_allocation_pct=0.30,
        max_daily_loss_pct=0.03,
        max_drawdown_pct=0.10,
        min_avg_volume=0.0,
        earnings_blackout_days=2,
        cooldown_minutes=60,
        post_loss_cooldown_minutes=120,
        mode="dryrun",
    )


def test_invariant_5_daily_loss_breach_trips_emergency_stop(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.system_control.set(
            "daily_start_equity", "100000.0", updated_at=clock.now(), updated_by="test"
        )

    broker = _broker(equity=95_000.0)  # 5% daily loss vs. a 3% cap
    data_source = FakeMarketDataSource({}, clock=clock)
    service = _service(session_factory, data_source, broker, clock)

    service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        assert repos.system_control.get("emergency_stop") == "true"


def test_invariant_5_daily_loss_within_cap_leaves_emergency_stop_unset(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.system_control.set(
            "daily_start_equity", "100000.0", updated_at=clock.now(), updated_by="test"
        )

    broker = _broker(equity=99_000.0)  # 1% daily loss, under the 3% cap
    data_source = FakeMarketDataSource({}, clock=clock)
    service = _service(session_factory, data_source, broker, clock)

    service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        assert repos.system_control.get("emergency_stop", "false") == "false"


# --- Invariant 6: a risk_evaluation row exists for every non-HOLD decision -


def test_invariant_6_risk_evaluation_persisted_for_every_non_hold_decision(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({"AAPL": _trending_candles("AAPL")}, clock=clock)
    broker = DryRunBroker(clock=clock, market_open=True)
    service = ScanCycleService(
        watchlist=["AAPL"],
        data_source=data_source,
        indicators=IndicatorService(),
        decision_engine=DecisionEngine(
            Weights(technical=1.0, llm=0.0, portfolio=0.0),
            Thresholds(buy=0.2, sell=-0.2),
            default_order_value=1000.0,
            clock=clock,
        ),
        risk_engine=RiskEngine(default_rules()),
        position_sizer=PositionSizer(
            risk_fraction=0.01, atr_stop_mult=2.0, take_profit_mult=2.0, default_order_value=1000.0
        ),
        stop_monitor=StopMonitor(data_source, clock=clock, quote_staleness_seconds=300),
        broker=broker,
        session_factory=session_factory,
        clock=clock,
        trading_window=WINDOW,
        max_position_value=2000.0,
        buying_power_buffer_pct=0.05,
        max_portfolio_exposure_pct=0.80,
        max_sector_allocation_pct=0.30,
        max_daily_loss_pct=0.03,
        max_drawdown_pct=0.10,
        min_avg_volume=0.0,
        earnings_blackout_days=2,
        cooldown_minutes=60,
        post_loss_cooldown_minutes=120,
        mode="dryrun",
    )

    cycle_id = service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        decisions = list(
            session.scalars(
                select(tables.Decision).where(tables.Decision.scan_cycle_id == cycle_id)
            )
        )
        assert len(decisions) > 0
        non_hold = [d for d in decisions if d.action != "HOLD"]
        assert len(non_hold) > 0  # the trending fixture must actually produce a BUY
        for decision in non_hold:
            evaluation = repos.risk_evaluations.get_by_decision_id(decision.id)
            assert evaluation is not None, f"missing risk_evaluation for decision {decision.id}"
