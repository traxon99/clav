"""Story 2.4 — StopMonitor: fires SELL exits on stop-loss/take-profit breach,
independent of the decision/risk pipeline, and fails closed on bad data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from clav.clock import FakeClock
from clav.data import tables
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import Account, MarketClock, Order, PortfolioSnapshot, Position, Quote
from clav.domain.portfolio import PortfolioManager
from clav.interfaces.broker import Broker
from clav.interfaces.market_data import MarketDataSource
from clav.services.execution import ExecutionEngine
from clav.services.stop_monitor import StopMonitor

NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _broker(*, market_open: bool = True, fill_price: float = 88.0) -> MagicMock:
    broker = MagicMock(spec=Broker)
    broker.get_clock.return_value = MarketClock(
        timestamp=NOW, is_open=market_open, next_open=NOW, next_close=NOW
    )
    broker.get_account.return_value = Account(
        cash=10_000, buying_power=10_000, equity=10_000, portfolio_value=10_000
    )

    def _submit(request):
        return Order(
            client_order_id=request.client_order_id,
            broker_order_id="broker-1",
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            status="filled",
            submitted_at=NOW,
            updated_at=NOW,
            filled_qty=request.qty,
            filled_avg_price=fill_price,
        )

    broker.submit_order.side_effect = _submit
    return broker


def _quote_source(*, price: float, ts: datetime = NOW, is_stale: bool = False) -> MagicMock:
    source = MagicMock(spec=MarketDataSource)
    source.get_quote.return_value = Quote(symbol="AAPL", price=price, ts=ts, is_stale=is_stale)
    return source


def _position(
    *,
    qty: int = 10,
    stop_price: float | None = 90.0,
    take_profit_price: float | None = 120.0,
) -> Position:
    return Position(
        symbol="AAPL",
        qty=qty,
        avg_entry_price=100.0,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
    )


def _snapshot(position: Position) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        ts=NOW, cash=10_000, equity=10_000, buying_power=10_000, positions=[position]
    )


def _seed_position(repos: Repositories, position: Position) -> None:
    instrument = repos.instruments.get_or_create(position.symbol)
    repos.positions.upsert(instrument.id, position, opened_at=NOW)
    repos.scan_cycles.create("cycle-1", started_at=NOW, mode="dryrun", trigger="manual")


# --- stop-loss / take-profit breach -----------------------------------------


def test_price_crossing_stop_emits_one_sell_order_and_closes_the_position(session_factory) -> None:
    broker = _broker(fill_price=88.0)
    data_source = _quote_source(price=88.0)  # <= stop_price 90 -> breach

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_position(repos, _position())
        execution = ExecutionEngine(broker, repos, clock=FakeClock(NOW))
        portfolio = PortfolioManager(repos, clock=FakeClock(NOW))
        monitor = StopMonitor(data_source, clock=FakeClock(NOW), quote_staleness_seconds=300)

        monitor.check("cycle-1", repos, execution, portfolio, _snapshot(_position()), frozenset())

    broker.submit_order.assert_called_once()
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_by_symbol("AAPL")
        assert repos.positions.get(instrument.id) is None  # fully sold, position closed

        decision_rows = list(session.scalars(select(tables.Decision)))
        assert len(decision_rows) == 1
        assert decision_rows[0].action == "SELL"
        assert decision_rows[0].reasoning["trigger"] == "stop_loss"


def test_price_crossing_take_profit_emits_one_sell_order(session_factory) -> None:
    broker = _broker(fill_price=125.0)
    data_source = _quote_source(price=125.0)  # >= take_profit_price 120 -> breach

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_position(repos, _position())
        execution = ExecutionEngine(broker, repos, clock=FakeClock(NOW))
        portfolio = PortfolioManager(repos, clock=FakeClock(NOW))
        monitor = StopMonitor(data_source, clock=FakeClock(NOW), quote_staleness_seconds=300)

        monitor.check("cycle-1", repos, execution, portfolio, _snapshot(_position()), frozenset())

    broker.submit_order.assert_called_once()


def test_price_between_stop_and_take_profit_is_a_noop(session_factory) -> None:
    broker = _broker()
    data_source = _quote_source(price=105.0)  # inside the 90..120 band

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_position(repos, _position())
        execution = ExecutionEngine(broker, repos, clock=FakeClock(NOW))
        portfolio = PortfolioManager(repos, clock=FakeClock(NOW))
        monitor = StopMonitor(data_source, clock=FakeClock(NOW), quote_staleness_seconds=300)

        monitor.check("cycle-1", repos, execution, portfolio, _snapshot(_position()), frozenset())

    broker.submit_order.assert_not_called()


# --- fail-closed on bad data -------------------------------------------------


def test_stale_quote_flag_skips_action(session_factory) -> None:
    broker = _broker()
    data_source = _quote_source(price=88.0, is_stale=True)

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_position(repos, _position())
        execution = ExecutionEngine(broker, repos, clock=FakeClock(NOW))
        portfolio = PortfolioManager(repos, clock=FakeClock(NOW))
        monitor = StopMonitor(data_source, clock=FakeClock(NOW), quote_staleness_seconds=300)

        monitor.check("cycle-1", repos, execution, portfolio, _snapshot(_position()), frozenset())

    broker.submit_order.assert_not_called()


def test_quote_older_than_staleness_threshold_skips_action(session_factory) -> None:
    broker = _broker()
    old_ts = NOW - timedelta(seconds=600)
    data_source = _quote_source(price=88.0, ts=old_ts)

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_position(repos, _position())
        execution = ExecutionEngine(broker, repos, clock=FakeClock(NOW))
        portfolio = PortfolioManager(repos, clock=FakeClock(NOW))
        monitor = StopMonitor(data_source, clock=FakeClock(NOW), quote_staleness_seconds=300)

        monitor.check("cycle-1", repos, execution, portfolio, _snapshot(_position()), frozenset())

    broker.submit_order.assert_not_called()


def test_quote_fetch_failure_skips_action_without_raising(session_factory) -> None:
    broker = _broker()
    data_source = MagicMock(spec=MarketDataSource)
    data_source.get_quote.side_effect = ConnectionError("data outage")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_position(repos, _position())
        execution = ExecutionEngine(broker, repos, clock=FakeClock(NOW))
        portfolio = PortfolioManager(repos, clock=FakeClock(NOW))
        monitor = StopMonitor(data_source, clock=FakeClock(NOW), quote_staleness_seconds=300)

        monitor.check(  # should not raise
            "cycle-1", repos, execution, portfolio, _snapshot(_position()), frozenset()
        )

    broker.submit_order.assert_not_called()


# --- no-ops and order-safety guard ------------------------------------------


def test_position_with_no_stop_or_take_profit_never_fetches_a_quote(session_factory) -> None:
    broker = _broker()
    data_source = MagicMock(spec=MarketDataSource)

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        position = _position(stop_price=None, take_profit_price=None)
        _seed_position(repos, position)
        execution = ExecutionEngine(broker, repos, clock=FakeClock(NOW))
        portfolio = PortfolioManager(repos, clock=FakeClock(NOW))
        monitor = StopMonitor(data_source, clock=FakeClock(NOW), quote_staleness_seconds=300)

        monitor.check("cycle-1", repos, execution, portfolio, _snapshot(position), frozenset())

    data_source.get_quote.assert_not_called()
    broker.submit_order.assert_not_called()


def test_zero_qty_position_is_a_noop(session_factory) -> None:
    broker = _broker()
    data_source = MagicMock(spec=MarketDataSource)

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        execution = ExecutionEngine(broker, repos, clock=FakeClock(NOW))
        portfolio = PortfolioManager(repos, clock=FakeClock(NOW))
        monitor = StopMonitor(data_source, clock=FakeClock(NOW), quote_staleness_seconds=300)

        monitor.check(
            "cycle-1", repos, execution, portfolio, _snapshot(_position(qty=0)), frozenset()
        )

    data_source.get_quote.assert_not_called()
    broker.submit_order.assert_not_called()


def test_skips_when_an_open_sell_order_already_exists_for_the_symbol(session_factory) -> None:
    broker = _broker(fill_price=88.0)
    data_source = _quote_source(price=88.0)

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_position(repos, _position())
        execution = ExecutionEngine(broker, repos, clock=FakeClock(NOW))
        portfolio = PortfolioManager(repos, clock=FakeClock(NOW))
        monitor = StopMonitor(data_source, clock=FakeClock(NOW), quote_staleness_seconds=300)

        monitor.check(
            "cycle-1",
            repos,
            execution,
            portfolio,
            _snapshot(_position()),
            frozenset({("AAPL", "sell")}),
        )

    broker.submit_order.assert_not_called()
