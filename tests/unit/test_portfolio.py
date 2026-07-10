from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from clav.clock import FakeClock
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import Account, Fill, OrderRequest, Position
from clav.domain.portfolio import PortfolioManager
from clav.interfaces.broker import Broker

NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _seed_order(
    repos: Repositories, *, client_order_id: str, symbol: str, side: str, qty: int
) -> None:
    instrument = repos.instruments.get_or_create(symbol)
    req = OrderRequest(client_order_id=client_order_id, symbol=symbol, side=side, qty=qty)
    repos.orders.create(
        instrument_id=instrument.id, decision_id=None, request=req, submitted_at=NOW
    )


def _fill(client_order_id: str, qty: int, price: float) -> Fill:
    return Fill(client_order_id=client_order_id, qty=qty, price=price, filled_at=NOW)


# --- apply_fill: open -> add -> partial-close -> close ---------------------


def test_full_position_lifecycle_pl_math(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        pm = PortfolioManager(repos, clock=FakeClock(NOW))

        # open: BUY 10 @ 100
        _seed_order(repos, client_order_id="c1-AAPL-buy", symbol="AAPL", side="buy", qty=10)
        pm.apply_fill(_fill("c1-AAPL-buy", 10, 100.0))

        position = repos.positions.get(repos.instruments.get_by_symbol("AAPL").id)
        assert position.qty == 10
        assert position.avg_entry_price == 100.0

        trade = repos.trades.get_open_trade(repos.instruments.get_by_symbol("AAPL").id)
        assert trade.qty == 10
        assert trade.entry_price == 100.0

        # add: BUY 10 @ 110 -> weighted avg entry = (1000 + 1100) / 20 = 105.0
        _seed_order(repos, client_order_id="c2-AAPL-buy", symbol="AAPL", side="buy", qty=10)
        pm.apply_fill(_fill("c2-AAPL-buy", 10, 110.0))

        instrument_id = repos.instruments.get_by_symbol("AAPL").id
        position = repos.positions.get(instrument_id)
        assert position.qty == 20
        assert position.avg_entry_price == pytest.approx(105.0)

        trade = repos.trades.get_open_trade(instrument_id)
        assert trade.qty == 20
        assert trade.entry_price == pytest.approx(105.0)

        # partial close: SELL 10 @ 120 -> pl = (120-105)*10 = 150
        _seed_order(repos, client_order_id="c3-AAPL-sell", symbol="AAPL", side="sell", qty=10)
        pm.apply_fill(_fill("c3-AAPL-sell", 10, 120.0))

        position = repos.positions.get(instrument_id)
        assert position.qty == 10
        assert position.avg_entry_price == pytest.approx(105.0)  # unchanged by a sell

        trade = repos.trades.get_open_trade(instrument_id)
        assert trade.status == "open"
        assert trade.qty == 10
        assert trade.realized_pl == pytest.approx(150.0)

        # full close: SELL 10 @ 130 -> additional pl = (130-105)*10 = 250; total = 400
        _seed_order(repos, client_order_id="c4-AAPL-sell", symbol="AAPL", side="sell", qty=10)
        pm.apply_fill(_fill("c4-AAPL-sell", 10, 130.0))

        assert repos.positions.get(instrument_id) is None
        closed_trade_id = trade.id
        closed = session.get(type(trade), closed_trade_id)
        assert closed.status == "closed"
        assert closed.realized_pl == pytest.approx(400.0)
        assert closed.exit_price == 130.0


def test_apply_fill_sell_with_no_position_is_a_safe_noop(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        pm = PortfolioManager(repos, clock=FakeClock(NOW))
        _seed_order(repos, client_order_id="c1-AAPL-sell", symbol="AAPL", side="sell", qty=5)

        pm.apply_fill(_fill("c1-AAPL-sell", 5, 100.0))  # should not raise

        assert repos.positions.get(repos.instruments.get_by_symbol("AAPL").id) is None


def test_apply_fill_unknown_client_order_id_raises(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        pm = PortfolioManager(repos, clock=FakeClock(NOW))
        with pytest.raises(ValueError, match="no order found"):
            pm.apply_fill(_fill("does-not-exist", 5, 100.0))


# --- snapshot() / reconcile(broker) -----------------------------------


def test_snapshot_before_any_reconcile_is_unreconciled_and_empty(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        pm = PortfolioManager(repos, clock=FakeClock(NOW))
        snap = pm.snapshot()

    assert snap.reconciled is False
    assert snap.cash == 0.0
    assert snap.positions == []


def test_reconcile_pulls_broker_state_and_marks_reconciled(session_factory) -> None:
    broker = MagicMock(spec=Broker)
    broker.get_account.return_value = Account(
        cash=5_000.0, buying_power=8_000.0, equity=9_000.0, portfolio_value=9_000.0
    )
    broker.get_positions.return_value = [
        Position(symbol="AAPL", qty=10, avg_entry_price=105.0)
    ]

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        pm = PortfolioManager(repos, clock=FakeClock(NOW))
        snap = pm.reconcile(broker)

    assert snap.reconciled is True
    assert snap.cash == 5_000.0
    assert snap.buying_power == 8_000.0
    assert len(snap.positions) == 1
    assert snap.positions[0].symbol == "AAPL"
    assert snap.gross_exposure == pytest.approx(1050.0)  # 10 * 105.0


def test_reconcile_removes_local_positions_broker_no_longer_reports(session_factory) -> None:
    broker = MagicMock(spec=Broker)
    broker.get_account.return_value = Account(
        cash=1000, buying_power=1000, equity=1000, portfolio_value=1000
    )
    broker.get_positions.return_value = []  # broker says flat

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        repos.positions.upsert(
            instrument.id, Position(symbol="AAPL", qty=10, avg_entry_price=100.0), opened_at=NOW
        )

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        pm = PortfolioManager(repos, clock=FakeClock(NOW))
        snap = pm.reconcile(broker)

    assert snap.positions == []
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        assert repos.positions.get(repos.instruments.get_by_symbol("AAPL").id) is None


def test_reconcile_failure_marks_snapshot_unreconciled(session_factory) -> None:
    broker = MagicMock(spec=Broker)
    broker.get_account.side_effect = ConnectionError("broker unreachable")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        pm = PortfolioManager(repos, clock=FakeClock(NOW))
        snap = pm.reconcile(broker)

    assert snap.reconciled is False


def test_reconcile_persists_a_snapshot_row(session_factory) -> None:
    broker = MagicMock(spec=Broker)
    broker.get_account.return_value = Account(
        cash=1000, buying_power=1000, equity=1000, portfolio_value=1000
    )
    broker.get_positions.return_value = []

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        pm = PortfolioManager(repos, clock=FakeClock(NOW))
        pm.reconcile(broker)

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        latest = repos.portfolio_snapshots.latest()
        assert latest is not None
        assert latest.reconciled is True
