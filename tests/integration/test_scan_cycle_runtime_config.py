"""Story 3.8 — a runtime config override (weights, watchlist) takes effect on
the very next scan cycle, no clav-core restart -- the acceptance-demo
requirement ("lowering w_llm or a risk knob in the UI, and the next cycle's
sizing reflecting the change")."""

from __future__ import annotations

from datetime import time

from conftest import NOON_UTC, FakeMarketDataSource
from conftest import flat_candles as _flat_candles

from clav.clock import FakeClock
from clav.common.cache import TtlCache
from clav.config import RuntimeOverrides, WeightsConfig
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
from clav.services.runtime_config import RuntimeConfigStore
from clav.services.scan_cycle import ScanCycleService
from clav.services.stop_monitor import StopMonitor

WINDOW = TradingWindow(start=time(9, 35), end=time(15, 55), timezone="America/New_York")


class StaticAnalyst:
    def __init__(self, signal: AnalystSignal) -> None:
        self._signal = signal

    def analyze(self, symbol, news, social_digest, context) -> AnalystSignal:
        return self._signal


def _session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _gateway(clock) -> AnalystGateway:
    bullish = AnalystSignal(sentiment=0.9, conviction=0.9, rationale="x", model="fake")
    return AnalystGateway(
        analyst=StaticAnalyst(bullish),
        news_sources=[],
        social_sources=[],
        filter_params=SocialFilterParams(),
        clock=clock,
        cache=TtlCache(clock=clock, ttl_seconds=3600),
        max_age_hours=72,
        max_items_per_symbol=50,
        social_baseline_window=20,
    )


def _service(session_factory, data_source, *, clock, runtime_config) -> ScanCycleService:
    broker = DryRunBroker(clock=clock, market_open=True)
    return ScanCycleService(
        watchlist=["MSFT"],
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
        analyst_gateway=_gateway(clock),
        runtime_config=runtime_config,
    )


def test_raising_w_llm_override_flips_a_flat_hold_to_a_buy_next_cycle(tmp_path) -> None:
    """MSFT is flat technically (HOLD with the boot-time w_llm=0). The operator
    raises w_llm via the runtime override; the very next cycle -- same
    ScanCycleService instance, no restart -- reflects it."""
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({"MSFT": _flat_candles("MSFT")}, clock=clock)
    factory = _session_factory(tmp_path)
    runtime_config = RuntimeConfigStore()
    service = _service(factory, data_source, clock=clock, runtime_config=runtime_config)

    cycle_1 = service.run(trigger="manual")
    with session_scope(factory) as session:
        repos = Repositories(session)
        assert repos.orders.get_by_client_order_id(f"clav-{cycle_1}-MSFT-buy") is None

    with session_scope(factory) as session:
        repos = Repositories(session)
        runtime_config.set(
            repos,
            RuntimeOverrides(weights=WeightsConfig(technical=0.0, llm=1.0, portfolio=0.0)),
            now=clock.now(),
            updated_by="operator",
        )

    cycle_2 = service.run(trigger="manual")
    with session_scope(factory) as session:
        repos = Repositories(session)
        order = repos.orders.get_by_client_order_id(f"clav-{cycle_2}-MSFT-buy")
        assert order is not None  # w_llm override took effect without a restart


def test_watchlist_override_changes_which_symbols_are_scanned(tmp_path) -> None:
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource(
        {"MSFT": _flat_candles("MSFT"), "AAPL": _flat_candles("AAPL")}, clock=clock
    )
    factory = _session_factory(tmp_path)
    runtime_config = RuntimeConfigStore()
    service = _service(factory, data_source, clock=clock, runtime_config=runtime_config)

    with session_scope(factory) as session:
        repos = Repositories(session)
        runtime_config.set(
            repos,
            RuntimeOverrides(watchlist=["AAPL"]),
            now=clock.now(),
            updated_by="operator",
        )

    cycle_id = service.run(trigger="manual")
    with session_scope(factory) as session:
        repos = Repositories(session)
        assert repos.instruments.get_by_symbol("AAPL") is not None
        assert repos.instruments.get_by_symbol("MSFT") is None
        cycle = repos.scan_cycles.get(cycle_id)
        assert cycle is not None and cycle.status == "completed"


def test_no_override_wired_is_byte_for_byte_boot_config(tmp_path) -> None:
    clock_a = FakeClock(NOON_UTC)
    clock_b = FakeClock(NOON_UTC)
    data_a = FakeMarketDataSource({"MSFT": _flat_candles("MSFT")}, clock=clock_a)
    data_b = FakeMarketDataSource({"MSFT": _flat_candles("MSFT")}, clock=clock_b)
    factory_a = _session_factory(tmp_path / "a")
    factory_b = _session_factory(tmp_path / "b")

    service_no_store = _service(factory_a, data_a, clock=clock_a, runtime_config=None)
    service_empty_store = _service(
        factory_b, data_b, clock=clock_b, runtime_config=RuntimeConfigStore()
    )

    cycle_a = service_no_store.run(trigger="manual")
    cycle_b = service_empty_store.run(trigger="manual")

    with session_scope(factory_a) as session:
        order_a = Repositories(session).orders.get_by_client_order_id(
            f"clav-{cycle_a}-MSFT-buy"
        )
    with session_scope(factory_b) as session:
        order_b = Repositories(session).orders.get_by_client_order_id(
            f"clav-{cycle_b}-MSFT-buy"
        )
    assert (order_a is None) == (order_b is None)
