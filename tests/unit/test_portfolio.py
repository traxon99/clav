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


# --- Story 2.3: stop-loss / take-profit persisted at entry ------------------


def test_apply_fill_buy_persists_stop_and_take_profit_on_new_open(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        pm = PortfolioManager(repos, clock=FakeClock(NOW))
        _seed_order(repos, client_order_id="c1-AAPL-buy", symbol="AAPL", side="buy", qty=10)

        pm.apply_fill(
            _fill("c1-AAPL-buy", 10, 100.0), stop_price=95.0, take_profit_price=110.0
        )

        position = repos.positions.get(repos.instruments.get_by_symbol("AAPL").id)
        assert position.stop_price == pytest.approx(95.0)
        assert position.take_profit_price == pytest.approx(110.0)


def test_apply_fill_buy_add_to_existing_position_keeps_original_stop(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        pm = PortfolioManager(repos, clock=FakeClock(NOW))

        _seed_order(repos, client_order_id="c1-AAPL-buy", symbol="AAPL", side="buy", qty=10)
        pm.apply_fill(_fill("c1-AAPL-buy", 10, 100.0), stop_price=95.0, take_profit_price=110.0)

        # adding to the position with a *different* sizing result shouldn't
        # overwrite the stop/take-profit set at the original entry.
        _seed_order(repos, client_order_id="c2-AAPL-buy", symbol="AAPL", side="buy", qty=10)
        pm.apply_fill(_fill("c2-AAPL-buy", 10, 110.0), stop_price=99.0, take_profit_price=130.0)

        position = repos.positions.get(repos.instruments.get_by_symbol("AAPL").id)
        assert position.qty == 20
        assert position.stop_price == pytest.approx(95.0)
        assert position.take_profit_price == pytest.approx(110.0)


def test_apply_fill_buy_without_sizing_result_leaves_stop_unset(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        pm = PortfolioManager(repos, clock=FakeClock(NOW))
        _seed_order(repos, client_order_id="c1-AAPL-buy", symbol="AAPL", side="buy", qty=10)

        pm.apply_fill(_fill("c1-AAPL-buy", 10, 100.0))  # ATR-unavailable fallback: no stop

        position = repos.positions.get(repos.instruments.get_by_symbol("AAPL").id)
        assert position.stop_price is None
        assert position.take_profit_price is None


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


# --- Story 2.2: market-value exposure, peak/drawdown, sector allocation ----


def test_reconcile_uses_broker_market_value_for_exposure_and_unrealized_pl(
    session_factory,
) -> None:
    broker = MagicMock(spec=Broker)
    broker.get_account.return_value = Account(
        cash=1_000.0, buying_power=1_000.0, equity=2_200.0, portfolio_value=2_200.0
    )
    broker.get_positions.return_value = [
        Position(
            symbol="AAPL", qty=10, avg_entry_price=100.0, market_value=1_200.0, unrealized_pl=200.0
        )
    ]

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        pm = PortfolioManager(repos, clock=FakeClock(NOW))
        snap = pm.reconcile(broker)

    # market value (1200), not cost basis (1000), drives exposure and P&L.
    assert snap.gross_exposure == pytest.approx(1_200.0)
    assert snap.net_exposure == pytest.approx(1_200.0)
    assert snap.unrealized_pl == pytest.approx(200.0)


def test_peak_equity_and_drawdown_track_a_rise_then_fall(session_factory) -> None:
    broker = MagicMock(spec=Broker)
    broker.get_positions.return_value = []

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        pm = PortfolioManager(repos, clock=FakeClock(NOW))

        broker.get_account.return_value = Account(
            cash=10_000, buying_power=10_000, equity=10_000, portfolio_value=10_000
        )
        snap = pm.reconcile(broker)
        assert snap.peak_equity == pytest.approx(10_000.0)
        assert snap.drawdown == pytest.approx(0.0)

        broker.get_account.return_value = Account(
            cash=12_000, buying_power=12_000, equity=12_000, portfolio_value=12_000
        )
        snap = pm.reconcile(broker)
        assert snap.peak_equity == pytest.approx(12_000.0)
        assert snap.drawdown == pytest.approx(0.0)

        broker.get_account.return_value = Account(
            cash=9_000, buying_power=9_000, equity=9_000, portfolio_value=9_000
        )
        snap = pm.reconcile(broker)
        # peak stays at the prior high-water mark; drawdown measures off it.
        assert snap.peak_equity == pytest.approx(12_000.0)
        assert snap.drawdown == pytest.approx((12_000.0 - 9_000.0) / 12_000.0)


def test_sector_allocation_grouped_by_instrument_sector(session_factory) -> None:
    broker = MagicMock(spec=Broker)
    broker.get_account.return_value = Account(
        cash=0, buying_power=0, equity=3_000.0, portfolio_value=3_000.0
    )
    broker.get_positions.return_value = [
        Position(symbol="AAPL", qty=10, avg_entry_price=100.0, market_value=1_000.0),
        Position(symbol="MSFT", qty=5, avg_entry_price=100.0, market_value=500.0),
        Position(symbol="UNTAGGED", qty=1, avg_entry_price=1_500.0, market_value=1_500.0),
    ]

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        aapl = repos.instruments.get_or_create("AAPL")
        aapl.sector = "Technology"
        msft = repos.instruments.get_or_create("MSFT")
        msft.sector = "Technology"
        repos.instruments.get_or_create("UNTAGGED")  # sector left unset

        pm = PortfolioManager(repos, clock=FakeClock(NOW))
        snap = pm.reconcile(broker)

    assert snap.sector_allocation == {
        "Technology": pytest.approx(1_500.0),
        "unknown": pytest.approx(1_500.0),
    }

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        latest = repos.portfolio_snapshots.latest()
        assert latest.sector_allocation == {"Technology": 1_500.0, "unknown": 1_500.0}


def test_daily_reset_rebases_peak_equity_and_sets_daily_start_equity(session_factory) -> None:
    broker = MagicMock(spec=Broker)
    broker.get_positions.return_value = []

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        pm = PortfolioManager(repos, clock=FakeClock(NOW))

        broker.get_account.return_value = Account(
            cash=20_000, buying_power=20_000, equity=20_000, portfolio_value=20_000
        )
        pm.reconcile(broker)  # establishes a 20k peak

        broker.get_account.return_value = Account(
            cash=15_000, buying_power=15_000, equity=15_000, portfolio_value=15_000
        )
        rebased = pm.daily_reset(broker)

        assert rebased.peak_equity == pytest.approx(15_000.0)
        assert rebased.drawdown == pytest.approx(0.0)
        assert repos.system_control.get("daily_start_equity") == "15000.0"

        # a normal cycle after the reset measures drawdown off the rebased peak.
        broker.get_account.return_value = Account(
            cash=14_000, buying_power=14_000, equity=14_000, portfolio_value=14_000
        )
        snap = pm.reconcile(broker)
        assert snap.peak_equity == pytest.approx(15_000.0)
        assert snap.drawdown == pytest.approx((15_000.0 - 14_000.0) / 15_000.0)


def test_daily_reset_skips_rebase_when_broker_unreachable(session_factory) -> None:
    broker = MagicMock(spec=Broker)
    broker.get_account.side_effect = ConnectionError("broker unreachable")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        pm = PortfolioManager(repos, clock=FakeClock(NOW))
        snap = pm.daily_reset(broker)

        assert snap.reconciled is False
        assert repos.system_control.get("daily_start_equity") is None


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
