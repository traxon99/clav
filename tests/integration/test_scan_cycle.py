"""Story 1.13 acceptance: drive a full scan cycle with DryRunBroker + fixture
candle data, end to end against a real temp SQLite DB."""

from datetime import time

import pytest
from conftest import (
    NOON_UTC,
    FakeMarketDataSource,
)
from conftest import (
    flat_candles as _flat_candles,
)
from conftest import (
    trending_candles as _trending_candles,
)

from clav.clock import FakeClock
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.decision import DecisionEngine, Thresholds, Weights
from clav.domain.indicators import IndicatorService
from clav.domain.models import Account
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import TradingWindow, default_rules
from clav.integrations.dryrun_broker import DryRunBroker
from clav.services.scan_cycle import ScanCycleService

WINDOW = TradingWindow(start=time(9, 35), end=time(15, 55), timezone="America/New_York")


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)


def _service(
    session_factory, data_source, *, watchlist, broker=None, clock=None
) -> ScanCycleService:
    clock = clock or FakeClock(NOON_UTC)
    broker = broker or DryRunBroker(clock=clock, market_open=True)
    return ScanCycleService(
        watchlist=watchlist,
        data_source=data_source,
        indicators=IndicatorService(),
        decision_engine=DecisionEngine(
            Weights(technical=1.0, llm=0.0, portfolio=0.0),
            Thresholds(buy=0.2, sell=-0.2),
            default_order_value=1000.0,
            clock=clock,
        ),
        risk_engine=RiskEngine(default_rules()),
        broker=broker,
        session_factory=session_factory,
        clock=clock,
        trading_window=WINDOW,
        max_position_value=2000.0,
        buying_power_buffer_pct=0.05,
        mode="dryrun",
    )


def test_full_cycle_persists_provenance_chain_and_submits_a_buy(session_factory) -> None:
    data_source = FakeMarketDataSource(
        {"AAPL": _trending_candles("AAPL"), "MSFT": _flat_candles("MSFT")},
        clock=FakeClock(NOON_UTC),
    )
    service = _service(session_factory, data_source, watchlist=["AAPL", "MSFT"])

    cycle_id = service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)

        cycle = repos.scan_cycles.get(cycle_id)
        assert cycle is not None
        assert cycle.status == "completed"
        assert cycle.market_open is True

        aapl = repos.instruments.get_by_symbol("AAPL")
        msft = repos.instruments.get_by_symbol("MSFT")
        assert aapl is not None
        assert msft is not None

        assert len(repos.candles.get_recent(aapl.id, "1Day", 100)) == 60
        assert len(repos.candles.get_recent(msft.id, "1Day", 100)) == 60

        # AAPL trended hard up -> BUY; MSFT flat -> HOLD
        aapl_order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-AAPL-buy")
        assert aapl_order is not None
        assert aapl_order.status == "accepted"  # DryRunBroker never fills

        msft_order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-MSFT-buy")
        assert msft_order is None

        snapshot = repos.portfolio_snapshots.latest()
        assert snapshot is not None


def test_cycle_skipped_when_market_closed(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    broker = DryRunBroker(clock=clock, market_open=False)
    data_source = FakeMarketDataSource(
        {"AAPL": _trending_candles("AAPL")}, clock=clock, market_open=False
    )
    service = _service(session_factory, data_source, watchlist=["AAPL"], broker=broker, clock=clock)

    cycle_id = service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        cycle = repos.scan_cycles.get(cycle_id)
        assert cycle is not None
        assert cycle.status == "skipped_market_closed"
    assert data_source.calls == []  # never even tried to fetch data


def test_per_ticker_failure_does_not_abort_the_cycle(session_factory) -> None:
    data_source = FakeMarketDataSource(
        {"AAPL": _trending_candles("AAPL"), "MSFT": _trending_candles("MSFT")},
        clock=FakeClock(NOON_UTC),
        fail_symbols=frozenset({"AAPL"}),
    )
    service = _service(session_factory, data_source, watchlist=["AAPL", "MSFT"])

    cycle_id = service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        cycle = repos.scan_cycles.get(cycle_id)
        assert cycle is not None
        assert cycle.status == "completed"  # AAPL's failure didn't abort the cycle

        assert repos.instruments.get_by_symbol("AAPL") is None  # never got far enough to persist
        msft = repos.instruments.get_by_symbol("MSFT")
        assert msft is not None
        msft_order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-MSFT-buy")
        assert msft_order is not None


def test_emergency_stop_blocks_new_entries_but_cycle_still_completes(session_factory) -> None:
    clock = FakeClock(NOON_UTC)

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.system_control.set(
            "emergency_stop", "true", updated_at=clock.now(), updated_by="test"
        )

    data_source = FakeMarketDataSource({"AAPL": _trending_candles("AAPL")}, clock=clock)
    service = _service(session_factory, data_source, watchlist=["AAPL"], clock=clock)

    cycle_id = service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        cycle = repos.scan_cycles.get(cycle_id)
        assert cycle is not None
        assert cycle.status == "completed"

        aapl = repos.instruments.get_by_symbol("AAPL")
        assert aapl is not None  # decision was still recorded

        order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-AAPL-buy")
        assert order is None  # EmergencyStopRule vetoed it


def test_daily_reset_rebases_peak_equity_via_the_service(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    broker = DryRunBroker(
        clock=clock,
        account=Account(cash=50_000, buying_power=50_000, equity=50_000, portfolio_value=50_000),
        market_open=True,
    )
    data_source = FakeMarketDataSource({"AAPL": _flat_candles("AAPL")}, clock=clock)
    service = _service(session_factory, data_source, watchlist=["AAPL"], broker=broker, clock=clock)

    service.daily_reset()

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        snapshot = repos.portfolio_snapshots.latest()
        assert snapshot is not None
        assert snapshot.equity == pytest.approx(50_000.0)
        assert snapshot.peak_equity == pytest.approx(50_000.0)
        assert snapshot.drawdown == pytest.approx(0.0)
        assert repos.system_control.get("daily_start_equity") == "50000.0"
