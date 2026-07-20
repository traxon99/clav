"""Story 1.13 acceptance: drive a full scan cycle with DryRunBroker + fixture
candle data, end to end against a real temp SQLite DB."""

from datetime import time, timedelta
from unittest.mock import MagicMock

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
from clav.domain.models import (
    Account,
    EarningsEvent,
    MarketClock,
    Order,
    OrderRequest,
    Position,
    Quote,
)
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import TradingWindow, default_rules
from clav.domain.risk.sizing import PositionSizer
from clav.integrations.dryrun_broker import DryRunBroker
from clav.interfaces.broker import Broker
from clav.services.scan_cycle import ScanCycleService
from clav.services.stop_monitor import StopMonitor

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
    session_factory,
    data_source,
    *,
    watchlist,
    broker=None,
    clock=None,
    alert_hook=None,
    sector_map=None,
    min_avg_volume=0.0,
    earnings_calendar=None,
    cooldown_minutes=60,
    post_loss_cooldown_minutes=120,
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
        min_avg_volume=min_avg_volume,
        earnings_blackout_days=2,
        cooldown_minutes=cooldown_minutes,
        post_loss_cooldown_minutes=post_loss_cooldown_minutes,
        mode="dryrun",
        alert_hook=alert_hook,
        sector_map=sector_map,
        earnings_calendar=earnings_calendar,
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


def test_stop_monitor_exits_a_breached_position_even_with_emergency_stop_set(
    session_factory,
) -> None:
    clock = FakeClock(NOON_UTC)

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.system_control.set(
            "emergency_stop", "true", updated_at=clock.now(), updated_by="test"
        )

    held_position = Position(
        symbol="AAPL", qty=10, avg_entry_price=100.0, stop_price=90.0, take_profit_price=120.0
    )
    broker = DryRunBroker(clock=clock, market_open=True, positions=[held_position])
    data_source = FakeMarketDataSource(
        {},
        clock=clock,
        quotes_by_symbol={"AAPL": Quote(symbol="AAPL", price=85.0, ts=NOON_UTC, is_stale=False)},
    )
    service = _service(session_factory, data_source, watchlist=[], broker=broker, clock=clock)

    cycle_id = service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        # the stop-monitor's exit was never vetoed by EmergencyStopRule, because
        # it never goes through the risk pipeline at all (Story 2.4).
        order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-AAPL-sell")
        assert order is not None
        assert order.side == "sell"
        assert order.qty == 10


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


# --- Story 2.3: ATR-based sizing + stop/take-profit persisted at entry -----


def test_atr_sizing_sets_qty_and_persists_stop_take_profit_on_the_new_position(
    session_factory,
) -> None:
    clock = FakeClock(NOON_UTC)
    broker = MagicMock(spec=Broker)
    broker.get_clock.return_value = MarketClock(
        timestamp=NOON_UTC, is_open=True, next_open=NOON_UTC, next_close=NOON_UTC
    )
    broker.get_account.return_value = Account(
        cash=100_000, buying_power=100_000, equity=100_000, portfolio_value=100_000
    )
    broker.get_positions.return_value = []

    def _submit(request):
        return Order(
            client_order_id=request.client_order_id,
            broker_order_id="broker-1",
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            status="filled",
            submitted_at=clock.now(),
            updated_at=clock.now(),
            filled_qty=request.qty,
            filled_avg_price=220.0,
        )

    broker.submit_order.side_effect = _submit

    data_source = FakeMarketDataSource({"AAPL": _trending_candles("AAPL")}, clock=clock)
    service = _service(session_factory, data_source, watchlist=["AAPL"], broker=broker, clock=clock)

    service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_by_symbol("AAPL")
        assert instrument is not None
        position = repos.positions.get(instrument.id)
        assert position is not None
        assert position.qty > 0
        # max_position_value=2000 @ ~220/share is the binding clamp, well below
        # the unclamped ATR raw_qty — proves the budget clamp is actually wired.
        assert position.qty <= 2000 // 220 + 1
        assert position.stop_price is not None
        assert position.stop_price < 220.0
        assert position.take_profit_price is not None
        assert position.take_profit_price > 220.0


# --- Story 2.5: portfolio-state circuit breakers ----------------------------


def test_daily_loss_breach_auto_trips_emergency_stop_and_fires_alert(session_factory) -> None:
    clock = FakeClock(NOON_UTC)

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.system_control.set(
            "daily_start_equity", "50000.0", updated_at=clock.now(), updated_by="test"
        )

    broker = DryRunBroker(
        clock=clock,
        account=Account(cash=47_000, buying_power=47_000, equity=47_000, portfolio_value=47_000),
        market_open=True,
    )
    data_source = FakeMarketDataSource({}, clock=clock)
    alert_hook = MagicMock()
    service = _service(
        session_factory,
        data_source,
        watchlist=[],
        broker=broker,
        clock=clock,
        alert_hook=alert_hook,
    )

    service.run(trigger="manual")  # 6% daily loss vs. a 3% max_daily_loss_pct cap

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        assert repos.system_control.get("emergency_stop") == "true"
    alert_hook.assert_called_once()


def test_daily_loss_within_cap_does_not_trip_emergency_stop(session_factory) -> None:
    clock = FakeClock(NOON_UTC)

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.system_control.set(
            "daily_start_equity", "50000.0", updated_at=clock.now(), updated_by="test"
        )

    broker = DryRunBroker(
        clock=clock,
        account=Account(cash=49_500, buying_power=49_500, equity=49_500, portfolio_value=49_500),
        market_open=True,
    )
    data_source = FakeMarketDataSource({}, clock=clock)
    service = _service(session_factory, data_source, watchlist=[], broker=broker, clock=clock)

    service.run(trigger="manual")  # 1% daily loss, under the 3% cap

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        assert repos.system_control.get("emergency_stop", "false") == "false"


def test_max_drawdown_rule_vetoes_new_entries_after_a_drawdown_from_peak(session_factory) -> None:
    clock = FakeClock(NOON_UTC)

    high_broker = DryRunBroker(
        clock=clock,
        account=Account(
            cash=100_000, buying_power=100_000, equity=100_000, portfolio_value=100_000
        ),
        market_open=True,
    )
    baseline_service = _service(
        session_factory,
        FakeMarketDataSource({}, clock=clock),
        watchlist=[],
        broker=high_broker,
        clock=clock,
    )
    baseline_service.run(trigger="manual")  # establishes a 100k peak_equity

    clock.advance(timedelta(minutes=30))
    low_broker = DryRunBroker(
        clock=clock,
        account=Account(cash=85_000, buying_power=85_000, equity=85_000, portfolio_value=85_000),
        market_open=True,
    )
    data_source = FakeMarketDataSource({"AAPL": _trending_candles("AAPL")}, clock=clock)
    service = _service(
        session_factory, data_source, watchlist=["AAPL"], broker=low_broker, clock=clock
    )

    # 15% drawdown from the 100k peak vs. a 10% max_drawdown_pct cap
    cycle_id = service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-AAPL-buy")
        assert order is None  # MaxDrawdownRule vetoed it


# --- Story 2.6: sector tagging ----------------------------------------------


def test_sector_map_seeds_instrument_sector_on_first_creation(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource(
        {"AAPL": _flat_candles("AAPL"), "XOM": _flat_candles("XOM")}, clock=clock
    )
    service = _service(
        session_factory,
        data_source,
        watchlist=["AAPL", "XOM"],
        clock=clock,
        sector_map={"AAPL": "Technology"},  # XOM deliberately left untagged
    )

    service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        assert repos.instruments.get_by_symbol("AAPL").sector == "Technology"
        assert repos.instruments.get_by_symbol("XOM").sector is None


def test_sector_map_does_not_overwrite_an_already_tagged_instrument(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        instrument.sector = "Manual Override"

    data_source = FakeMarketDataSource({"AAPL": _flat_candles("AAPL")}, clock=clock)
    service = _service(
        session_factory,
        data_source,
        watchlist=["AAPL"],
        clock=clock,
        sector_map={"AAPL": "Technology"},
    )

    service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        assert repos.instruments.get_by_symbol("AAPL").sector == "Manual Override"


# --- Story 2.7: data-integrity rules ----------------------------------------


def test_data_freshness_rule_vetoes_a_buy_on_stale_candle_data(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    candles = _trending_candles("AAPL")
    candles[-1] = candles[-1].model_copy(update={"is_stale": True})
    data_source = FakeMarketDataSource({"AAPL": candles}, clock=clock)
    service = _service(session_factory, data_source, watchlist=["AAPL"], clock=clock)

    cycle_id = service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-AAPL-buy")
        assert order is None  # DataFreshnessRule vetoed it


def test_min_liquidity_rule_vetoes_a_buy_on_thin_volume(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({"AAPL": _trending_candles("AAPL")}, clock=clock)
    # fixture candles carry a fixed volume=1000, well under this cap
    service = _service(
        session_factory, data_source, watchlist=["AAPL"], clock=clock, min_avg_volume=100_000.0
    )

    cycle_id = service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-AAPL-buy")
        assert order is None  # MinLiquidityRule vetoed it


# --- Story 2.8: earnings blackout --------------------------------------


def test_startup_reconcile_seeds_the_earnings_calendar(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({}, clock=clock)
    calendar = [
        EarningsEvent(
            symbol="AAPL",
            event_type="earnings",
            scheduled_at=NOON_UTC + timedelta(days=1),
            confirmed=True,
            source="config_seed",
        )
    ]
    service = _service(
        session_factory, data_source, watchlist=[], clock=clock, earnings_calendar=calendar
    )

    service.startup_reconcile()

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_by_symbol("AAPL")
        assert instrument is not None
        upcoming = repos.earnings_events.get_upcoming(instrument.id, after=NOON_UTC)
        assert len(upcoming) == 1
        assert upcoming[0].confirmed is True


def test_startup_reconcile_seeding_is_idempotent(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({}, clock=clock)
    calendar = [
        EarningsEvent(
            symbol="AAPL",
            event_type="earnings",
            scheduled_at=NOON_UTC + timedelta(days=1),
            confirmed=False,
            source="config_seed",
        )
    ]
    service = _service(
        session_factory, data_source, watchlist=[], clock=clock, earnings_calendar=calendar
    )

    service.startup_reconcile()
    service.startup_reconcile()  # simulate a process restart

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_by_symbol("AAPL")
        upcoming = repos.earnings_events.get_upcoming(instrument.id, after=NOON_UTC)
        assert len(upcoming) == 1  # not duplicated


def test_earnings_blackout_rule_vetoes_a_buy_within_the_window(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({"AAPL": _trending_candles("AAPL")}, clock=clock)
    calendar = [
        EarningsEvent(
            symbol="AAPL",
            event_type="earnings",
            scheduled_at=NOON_UTC + timedelta(days=1),  # inside the 2-day blackout window
            confirmed=False,
            source="config_seed",
        )
    ]
    service = _service(
        session_factory,
        data_source,
        watchlist=["AAPL"],
        clock=clock,
        earnings_calendar=calendar,
    )
    service.startup_reconcile()

    cycle_id = service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-AAPL-buy")
        assert order is None  # EarningsBlackoutRule vetoed it


def test_earnings_blackout_rule_allows_a_buy_outside_the_window(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({"AAPL": _trending_candles("AAPL")}, clock=clock)
    calendar = [
        EarningsEvent(
            symbol="AAPL",
            event_type="earnings",
            scheduled_at=NOON_UTC + timedelta(days=30),  # well outside the 2-day window
            confirmed=False,
            source="config_seed",
        )
    ]
    service = _service(
        session_factory,
        data_source,
        watchlist=["AAPL"],
        clock=clock,
        earnings_calendar=calendar,
    )
    service.startup_reconcile()

    cycle_id = service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-AAPL-buy")
        assert order is not None  # not in the blackout window


def test_earnings_blackout_rule_allows_a_buy_when_no_earnings_data_at_all(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({"AAPL": _trending_candles("AAPL")}, clock=clock)
    service = _service(session_factory, data_source, watchlist=["AAPL"], clock=clock)

    cycle_id = service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-AAPL-buy")
        assert order is not None  # fail-open: no known earnings, no blackout


# --- Story 2.9: cooldown -----------------------------------------------


def _seed_closed_trade(
    repos: Repositories, symbol: str, *, closed_at, realized_pl: float = 100.0
) -> None:
    instrument = repos.instruments.get_or_create(symbol)
    entry_req = OrderRequest(
        client_order_id=f"seed-{symbol}-entry", symbol=symbol, side="buy", qty=10
    )
    entry_order = repos.orders.create(
        instrument_id=instrument.id, decision_id=None, request=entry_req, submitted_at=closed_at
    )
    entry_order.status = "filled"
    trade = repos.trades.open_trade(
        instrument_id=instrument.id,
        entry_order_id=entry_order.id,
        entry_decision_id=None,
        qty=10,
        entry_price=100.0,
        opened_at=closed_at,
    )
    exit_req = OrderRequest(
        client_order_id=f"seed-{symbol}-exit", symbol=symbol, side="sell", qty=10
    )
    exit_order = repos.orders.create(
        instrument_id=instrument.id, decision_id=None, request=exit_req, submitted_at=closed_at
    )
    exit_order.status = "filled"
    repos.trades.close_trade(
        trade.id,
        exit_order_id=exit_order.id,
        exit_price=100.0 + realized_pl / 10,
        closed_at=closed_at,
        realized_pl=realized_pl,
        return_pct=realized_pl / 1000.0,
    )


def test_symbol_cooldown_vetoes_a_buy_within_the_window(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_closed_trade(repos, "AAPL", closed_at=NOON_UTC - timedelta(minutes=10))

    data_source = FakeMarketDataSource({"AAPL": _trending_candles("AAPL")}, clock=clock)
    service = _service(
        session_factory, data_source, watchlist=["AAPL"], clock=clock, cooldown_minutes=60
    )

    cycle_id = service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-AAPL-buy")
        assert order is None  # CooldownRule vetoed it


def test_symbol_cooldown_allows_a_buy_after_the_window(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_closed_trade(repos, "AAPL", closed_at=NOON_UTC - timedelta(minutes=90))

    data_source = FakeMarketDataSource({"AAPL": _trending_candles("AAPL")}, clock=clock)
    service = _service(
        session_factory, data_source, watchlist=["AAPL"], clock=clock, cooldown_minutes=60
    )

    cycle_id = service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-AAPL-buy")
        assert order is not None  # outside the per-symbol cooldown window


def test_post_loss_cooldown_freezes_all_symbols_then_releases(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        # the loss happened in a different symbol than the one we're testing
        _seed_closed_trade(
            repos, "TSLA", closed_at=NOON_UTC - timedelta(minutes=10), realized_pl=-100.0
        )

    data_source = FakeMarketDataSource({"AAPL": _trending_candles("AAPL")}, clock=clock)
    service = _service(
        session_factory,
        data_source,
        watchlist=["AAPL"],
        clock=clock,
        post_loss_cooldown_minutes=120,
    )

    cycle_id = service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-AAPL-buy")
        assert order is None  # global post-loss cooldown vetoed it, even for an unrelated symbol


def test_post_loss_cooldown_releases_after_the_window(session_factory) -> None:
    clock = FakeClock(NOON_UTC)
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_closed_trade(
            repos, "TSLA", closed_at=NOON_UTC - timedelta(minutes=150), realized_pl=-100.0
        )

    data_source = FakeMarketDataSource({"AAPL": _trending_candles("AAPL")}, clock=clock)
    service = _service(
        session_factory,
        data_source,
        watchlist=["AAPL"],
        clock=clock,
        post_loss_cooldown_minutes=120,
    )

    cycle_id = service.run(trigger="manual")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-AAPL-buy")
        assert order is not None  # outside the post-loss cooldown window
