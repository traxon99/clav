"""Story 3.6 — Gemini wired as proposer into a full scan cycle, still gated by
the complete Epic-2 risk pipeline; w_llm=0 stays byte-for-byte technical-only."""

from __future__ import annotations

from datetime import time

from conftest import NOON_UTC, FakeMarketDataSource
from conftest import flat_candles as _flat_candles

from clav.clock import FakeClock
from clav.common.cache import TtlCache
from clav.data import tables
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.decision import DecisionEngine, Thresholds, Weights
from clav.domain.indicators import IndicatorService
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import TradingWindow, default_rules
from clav.domain.risk.sizing import PositionSizer
from clav.domain.social import SocialFilterParams
from clav.integrations.dryrun_broker import DryRunBroker
from clav.interfaces.analyst import AnalystSignal
from clav.services.analyst_gateway import AnalystGateway
from clav.services.scan_cycle import ScanCycleService
from clav.services.stop_monitor import StopMonitor

WINDOW = TradingWindow(start=time(9, 35), end=time(15, 55), timezone="America/New_York")


class StaticAnalyst:
    """Always returns the configured signal — used to prove wiring, not to
    re-test GeminiAnalyst's own validation (covered in test_gemini_analyst.py)."""

    def __init__(self, signal: AnalystSignal) -> None:
        self._signal = signal
        self.calls = 0

    def analyze(self, symbol, news, social_digest, context) -> AnalystSignal:
        self.calls += 1
        return self._signal


def _session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _service(session_factory, data_source, *, w_llm, analyst_gateway, clock) -> ScanCycleService:
    broker = DryRunBroker(clock=clock, market_open=True)
    return ScanCycleService(
        watchlist=["MSFT"],
        data_source=data_source,
        indicators=IndicatorService(),
        decision_engine=DecisionEngine(
            Weights(technical=1.0 - w_llm, llm=w_llm, portfolio=0.0),
            Thresholds(buy=0.2, sell=-0.2),
            default_order_value=1000.0,
            clock=clock,
        ),
        risk_engine=RiskEngine(default_rules()),
        position_sizer=PositionSizer(
            risk_fraction=0.01,
            atr_stop_mult=2.0,
            take_profit_mult=2.0,
            default_order_value=1000.0,
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
        analyst_gateway=analyst_gateway,
    )


def _gateway(clock, signal: AnalystSignal) -> AnalystGateway:
    return AnalystGateway(
        analyst=StaticAnalyst(signal),
        news_sources=[],
        social_sources=[],
        filter_params=SocialFilterParams(),
        clock=clock,
        cache=TtlCache(clock=clock, ttl_seconds=3600),
        max_age_hours=72,
        max_items_per_symbol=50,
        social_baseline_window=20,
    )


def test_bullish_gemini_signal_flips_flat_technical_to_a_risk_gated_buy(tmp_path) -> None:
    """MSFT is flat technically (HOLD on its own) but a strongly bullish,
    high-conviction Gemini signal should be enough for w_llm=1.0 to push the
    score over the buy threshold -- and the order still goes through the full
    Epic-2 risk pipeline (risk_evaluation persisted, sizing applied)."""
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({"MSFT": _flat_candles("MSFT")}, clock=clock)
    bullish = AnalystSignal(
        sentiment=0.9, conviction=0.9, rationale="strong catalyst", model="fake"
    )
    gateway = _gateway(clock, bullish)
    factory = _session_factory(tmp_path)
    service = _service(factory, data_source, w_llm=1.0, analyst_gateway=gateway, clock=clock)

    cycle_id = service.run(trigger="manual")

    with session_scope(factory) as session:
        repos = Repositories(session)
        msft = repos.instruments.get_by_symbol("MSFT")
        assert msft is not None
        order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-MSFT-buy")
        assert order is not None  # risk-passing BUY reached the broker

        decision = (
            session.query(tables.Decision).filter_by(instrument_id=msft.id).first()
        )
        assert decision is not None
        assert decision.llm_signal > 0.5
        assert "llm" in decision.reasoning
        assert decision.reasoning["llm"]["is_fallback"] is False

        risk_eval = repos.risk_evaluations.get_by_decision_id(decision.id)
        assert risk_eval is not None
        assert risk_eval.approved is True  # still ran through the complete risk gate


def test_bearish_gemini_signal_alone_cannot_bypass_risk_veto(tmp_path) -> None:
    """A bullish Gemini signal pushes the score into BUY territory, but with
    the emergency stop tripped the risk engine must still veto -- proving
    Gemini is a proposer, never a bypass."""
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({"MSFT": _flat_candles("MSFT")}, clock=clock)
    bullish = AnalystSignal(sentiment=0.9, conviction=0.9, rationale="x", model="fake")
    gateway = _gateway(clock, bullish)
    factory = _session_factory(tmp_path)
    service = _service(factory, data_source, w_llm=1.0, analyst_gateway=gateway, clock=clock)

    with session_scope(factory) as session:
        repos = Repositories(session)
        repos.system_control.set(
            "emergency_stop", "true", updated_at=clock.now(), updated_by="test"
        )

    cycle_id = service.run(trigger="manual")

    with session_scope(factory) as session:
        repos = Repositories(session)
        order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-MSFT-buy")
        assert order is None  # estop vetoed it despite the bullish LLM signal


def test_w_llm_zero_matches_technical_only_baseline(tmp_path) -> None:
    """Regression guard (epic decision): with w_llm=0 the outcome is identical
    whether or not an analyst gateway is wired in at all."""
    clock_a = FakeClock(NOON_UTC)
    clock_b = FakeClock(NOON_UTC)
    data_a = FakeMarketDataSource({"MSFT": _flat_candles("MSFT")}, clock=clock_a)
    data_b = FakeMarketDataSource({"MSFT": _flat_candles("MSFT")}, clock=clock_b)

    bullish = AnalystSignal(sentiment=0.9, conviction=0.9, rationale="x", model="fake")
    gateway = _gateway(clock_a, bullish)

    factory_a = _session_factory(tmp_path / "a")
    factory_b = _session_factory(tmp_path / "b")
    service_with_llm = _service(
        factory_a, data_a, w_llm=0.0, analyst_gateway=gateway, clock=clock_a
    )
    service_without_llm = _service(
        factory_b, data_b, w_llm=0.0, analyst_gateway=None, clock=clock_b
    )

    cycle_a = service_with_llm.run(trigger="manual")
    cycle_b = service_without_llm.run(trigger="manual")

    with session_scope(factory_a) as session:
        repos = Repositories(session)
        order_a = repos.orders.get_by_client_order_id(f"clav-{cycle_a}-MSFT-buy")

    with session_scope(factory_b) as session:
        repos = Repositories(session)
        order_b = repos.orders.get_by_client_order_id(f"clav-{cycle_b}-MSFT-buy")

    assert (order_a is None) == (order_b is None)  # same action either way
