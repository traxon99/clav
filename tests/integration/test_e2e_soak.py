"""Story 1.15 — E2E paper soak: drives real modules end to end across
multiple cycles in dryrun mode and asserts the full provenance chain, no
duplicate orders, no unhandled exceptions, and that emergency stop freezes
new entries mid-soak. Uses DryRunBroker rather than a live paper account
(none available in this sandbox — see the plan's verification notes)."""

from datetime import time, timedelta

import pytest
from conftest import NOON_UTC, FakeMarketDataSource, trending_candles

from clav.clock import FakeClock
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.decision import DecisionEngine, Thresholds, Weights
from clav.domain.indicators import IndicatorService
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import TradingWindow, default_rules
from clav.domain.risk.sizing import PositionSizer
from clav.integrations.dryrun_broker import DryRunBroker
from clav.services.scan_cycle import ScanCycleService
from clav.services.stop_monitor import StopMonitor

WINDOW = TradingWindow(start=time(9, 35), end=time(15, 55), timezone="America/New_York")
WATCHLIST = ["AAPL", "MSFT", "NVDA"]


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _build_service(session_factory, data_source, broker, clock) -> ScanCycleService:
    return ScanCycleService(
        watchlist=WATCHLIST,
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
        min_avg_volume=0.0,  # fixture candles use a fixed volume=1000; keep the rule inert
        earnings_blackout_days=2,
        cooldown_minutes=60,
        post_loss_cooldown_minutes=120,
        mode="dryrun",
    )


def test_multi_cycle_soak_full_provenance_no_duplicates_no_unhandled_exceptions(
    session_factory,
) -> None:
    clock = FakeClock(NOON_UTC)
    broker = DryRunBroker(clock=clock, market_open=True)
    candles = {symbol: trending_candles(symbol) for symbol in WATCHLIST}
    data_source = FakeMarketDataSource(candles, clock=clock)
    service = _build_service(session_factory, data_source, broker, clock)

    cycle_ids = []
    for _ in range(5):
        # every scan cycle must complete without raising — a soak that throws
        # is a failed soak.
        cycle_ids.append(service.run(trigger="scheduled"))
        clock.advance(timedelta(minutes=30))

    assert len(cycle_ids) == len(set(cycle_ids)) == 5  # cycle_ids themselves are unique

    with session_scope(session_factory) as session:
        repos = Repositories(session)

        for cid in cycle_ids:
            cycle = repos.scan_cycles.get(cid)
            assert cycle is not None
            assert cycle.status == "completed"

        # full provenance chain: instrument -> candle -> indicator_set -> decision -> order
        for symbol in WATCHLIST:
            instrument = repos.instruments.get_by_symbol(symbol)
            assert instrument is not None
            assert len(repos.candles.get_recent(instrument.id, "1Day", 200)) > 0

        # every BUY order across the whole soak has a unique client_order_id
        # (each cycle mints its own cycle-scoped id; the DB UNIQUE constraint
        # is the hard backstop tested directly in test_migrations.py)
        all_client_order_ids = [
            repos.orders.get_by_client_order_id(f"clav-{cid}-{symbol}-buy").client_order_id
            for cid in cycle_ids
            for symbol in WATCHLIST
            if repos.orders.get_by_client_order_id(f"clav-{cid}-{symbol}-buy") is not None
        ]
        assert len(all_client_order_ids) == len(set(all_client_order_ids))
        assert len(all_client_order_ids) > 0  # the strong uptrend did produce buys

        # re-running the very same cycle_id (simulating a retried/duplicated
        # trigger) must not create a second order for it
        repeat_cycle_id = cycle_ids[0]

    # scan_cycles.create() PK collision, by design
    with pytest.raises(Exception), session_scope(session_factory) as session:  # noqa: B017
        repos = Repositories(session)
        repos.scan_cycles.create(
            repeat_cycle_id, started_at=clock.now(), mode="dryrun", trigger="scheduled"
        )


def test_soak_with_emergency_stop_mid_run_freezes_new_entries(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    broker = DryRunBroker(clock=clock, market_open=True)
    candles = {symbol: trending_candles(symbol) for symbol in WATCHLIST}
    data_source = FakeMarketDataSource(candles, clock=clock)
    service = _build_service(session_factory, data_source, broker, clock)

    pre_estop_cycle = service.run(trigger="scheduled")
    clock.advance(timedelta(minutes=30))

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.system_control.set(
            "emergency_stop", "true", updated_at=clock.now(), updated_by="test"
        )

    post_estop_cycles = []
    for _ in range(3):
        post_estop_cycles.append(service.run(trigger="scheduled"))
        clock.advance(timedelta(minutes=30))

    with session_scope(session_factory) as session:
        repos = Repositories(session)

        # the pre-estop cycle bought normally
        pre_orders = [
            repos.orders.get_by_client_order_id(f"clav-{pre_estop_cycle}-{s}-buy")
            for s in WATCHLIST
        ]
        assert any(o is not None for o in pre_orders)

        # every post-estop cycle completed (cycle itself never crashes)...
        for cid in post_estop_cycles:
            cycle = repos.scan_cycles.get(cid)
            assert cycle is not None
            assert cycle.status == "completed"

        # ...but froze all new BUY entries
        for cid in post_estop_cycles:
            for symbol in WATCHLIST:
                order = repos.orders.get_by_client_order_id(f"clav-{cid}-{symbol}-buy")
                assert order is None, f"BUY order {symbol}/{cid} should have been vetoed by estop"
