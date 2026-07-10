from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from clav.clock import FakeClock
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import (
    Account,
    MarketClock,
    Order,
    RiskDecision,
    TradeDecision,
)
from clav.interfaces.broker import Broker
from clav.services.execution import ExecutionEngine

NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _broker(*, market_open: bool = True, buying_power: float = 10_000.0) -> MagicMock:
    broker = MagicMock(spec=Broker)
    broker.get_clock.return_value = MarketClock(
        timestamp=NOW, is_open=market_open, next_open=NOW, next_close=NOW
    )
    broker.get_account.return_value = Account(
        cash=buying_power,
        buying_power=buying_power,
        equity=buying_power,
        portfolio_value=buying_power,
    )
    return broker


def _decision(
    action: str = "BUY", symbol: str = "AAPL", cycle_id: str = "cycle-1"
) -> TradeDecision:
    return TradeDecision(
        cycle_id=cycle_id,
        symbol=symbol,
        action=action,  # type: ignore[arg-type]
        target_qty=8,
        raw_score=0.5,
        technical_score=0.5,
        llm_signal=0.0,
        portfolio_bias=0.0,
    )


def _approved(qty: int = 8) -> RiskDecision:
    return RiskDecision(approved=True, adjusted_qty=qty)


def _filled_order(client_order_id: str, symbol: str, side: str, qty: int) -> Order:
    return Order(
        client_order_id=client_order_id,
        broker_order_id="broker-1",
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        qty=qty,
        status="filled",
        submitted_at=NOW,
        updated_at=NOW,
        filled_qty=qty,
        filled_avg_price=190.5,
    )


# --- validation gates -------------------------------------------------


def test_not_approved_returns_none_and_submits_nothing(session_factory) -> None:
    broker = _broker()
    with session_scope(session_factory) as session:
        engine = ExecutionEngine(broker, Repositories(session), clock=FakeClock(NOW))
        result = engine.execute(_decision(), RiskDecision(approved=False, adjusted_qty=0))
    assert result is None
    broker.submit_order.assert_not_called()


def test_zero_adjusted_qty_returns_none(session_factory) -> None:
    broker = _broker()
    with session_scope(session_factory) as session:
        engine = ExecutionEngine(broker, Repositories(session), clock=FakeClock(NOW))
        result = engine.execute(_decision(), RiskDecision(approved=True, adjusted_qty=0))
    assert result is None
    broker.submit_order.assert_not_called()


def test_hold_action_returns_none(session_factory) -> None:
    broker = _broker()
    with session_scope(session_factory) as session:
        engine = ExecutionEngine(broker, Repositories(session), clock=FakeClock(NOW))
        result = engine.execute(_decision(action="HOLD"), _approved())
    assert result is None
    broker.submit_order.assert_not_called()


def test_market_closed_blocks_execution(session_factory) -> None:
    broker = _broker(market_open=False)
    with session_scope(session_factory) as session:
        engine = ExecutionEngine(broker, Repositories(session), clock=FakeClock(NOW))
        result = engine.execute(_decision(), _approved())
    assert result is None
    broker.submit_order.assert_not_called()


def test_buy_blocked_when_no_buying_power(session_factory) -> None:
    broker = _broker(buying_power=0.0)
    with session_scope(session_factory) as session:
        engine = ExecutionEngine(broker, Repositories(session), clock=FakeClock(NOW))
        result = engine.execute(_decision(action="BUY"), _approved())
    assert result is None
    broker.submit_order.assert_not_called()


def test_sell_ignores_buying_power_check(session_factory) -> None:
    broker = _broker(buying_power=0.0)
    broker.submit_order.return_value = _filled_order("clav-cycle-1-AAPL-sell", "AAPL", "sell", 8)
    with session_scope(session_factory) as session:
        engine = ExecutionEngine(broker, Repositories(session), clock=FakeClock(NOW))
        result = engine.execute(_decision(action="SELL"), _approved())
    assert result is not None
    broker.get_account.assert_not_called()


# --- happy path + idempotency ----------------------------------------------


def test_successful_buy_persists_order_and_fill(session_factory) -> None:
    broker = _broker()
    broker.submit_order.return_value = _filled_order("clav-cycle-1-AAPL-buy", "AAPL", "buy", 8)

    with session_scope(session_factory) as session:
        engine = ExecutionEngine(broker, Repositories(session), clock=FakeClock(NOW))
        result = engine.execute(_decision(), _approved())

    assert result is not None
    assert result.status == "filled"

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        row = repos.orders.get_by_client_order_id("clav-cycle-1-AAPL-buy")
        assert row is not None
        assert row.status == "filled"
        assert session.query(type(row)).count() == 1


def test_double_submit_yields_exactly_one_order(session_factory) -> None:
    broker = _broker()
    broker.submit_order.return_value = _filled_order("clav-cycle-1-AAPL-buy", "AAPL", "buy", 8)

    with session_scope(session_factory) as session:
        engine = ExecutionEngine(broker, Repositories(session), clock=FakeClock(NOW))
        first = engine.execute(_decision(), _approved())

    with session_scope(session_factory) as session:
        engine = ExecutionEngine(broker, Repositories(session), clock=FakeClock(NOW))
        second = engine.execute(_decision(), _approved())

    assert first is not None
    assert second is not None
    assert first.client_order_id == second.client_order_id
    broker.submit_order.assert_called_once()  # second call never reached the broker

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        row = repos.orders.get_by_client_order_id("clav-cycle-1-AAPL-buy")
        count = session.query(type(row)).count()
        assert count == 1


def test_permanent_broker_error_marks_order_failed_and_fires_alert(session_factory) -> None:
    broker = _broker()
    broker.submit_order.side_effect = ValueError("account restricted")
    alerts: list[tuple[str, str]] = []

    with session_scope(session_factory) as session:
        engine = ExecutionEngine(
            broker,
            Repositories(session),
            clock=FakeClock(NOW),
            alert_hook=lambda coid, msg: alerts.append((coid, msg)),
        )
        result = engine.execute(_decision(), _approved())

    assert result is None
    assert alerts == [("clav-cycle-1-AAPL-buy", "account restricted")]

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        row = repos.orders.get_by_client_order_id("clav-cycle-1-AAPL-buy")
        assert row is not None
        assert row.status == "failed"
        assert row.error == "account restricted"


# --- reconcile() ------------------------------------------------------


def test_reconcile_updates_open_order_from_broker_and_captures_fill(session_factory) -> None:
    broker = _broker()
    from clav.domain.models import OrderRequest

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        req = OrderRequest(client_order_id="clav-c1-AAPL-buy", symbol="AAPL", side="buy", qty=8)
        repos.orders.create(
            instrument_id=instrument.id, decision_id=None, request=req, submitted_at=NOW
        )

    broker.get_order.return_value = _filled_order("clav-c1-AAPL-buy", "AAPL", "buy", 8)

    with session_scope(session_factory) as session:
        engine = ExecutionEngine(broker, Repositories(session), clock=FakeClock(NOW))
        engine.reconcile()

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        row = repos.orders.get_by_client_order_id("clav-c1-AAPL-buy")
        assert row is not None
        assert row.status == "filled"
        # exactly one order row: reconcile updates, never inserts
        assert session.query(type(row)).count() == 1


def test_reconcile_marks_order_failed_when_missing_on_broker(session_factory) -> None:
    broker = _broker()
    from clav.domain.models import OrderRequest

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        req = OrderRequest(client_order_id="clav-c1-AAPL-buy", symbol="AAPL", side="buy", qty=8)
        repos.orders.create(
            instrument_id=instrument.id, decision_id=None, request=req, submitted_at=NOW
        )

    broker.get_order.return_value = None

    with session_scope(session_factory) as session:
        engine = ExecutionEngine(broker, Repositories(session), clock=FakeClock(NOW))
        engine.reconcile()

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        row = repos.orders.get_by_client_order_id("clav-c1-AAPL-buy")
        assert row is not None
        assert row.status == "failed"


def test_reconcile_noop_when_no_open_orders(session_factory) -> None:
    broker = _broker()
    with session_scope(session_factory) as session:
        engine = ExecutionEngine(broker, Repositories(session), clock=FakeClock(NOW))
        engine.reconcile()  # should not raise
    broker.get_order.assert_not_called()


def test_restart_with_open_order_reconciles_without_duplicate(session_factory) -> None:
    """Simulates the Story 1.11 acceptance scenario: an order was open when the
    process died; on restart, reconcile() must sync it, and a subsequent
    execute() for the same decision must not create a second order."""
    broker = _broker()
    broker.submit_order.return_value = _filled_order("clav-cycle-1-AAPL-buy", "AAPL", "buy", 8)

    with session_scope(session_factory) as session:
        engine = ExecutionEngine(broker, Repositories(session), clock=FakeClock(NOW))
        engine.execute(_decision(), _approved())

    # "restart": reconcile against the broker, which now reports it filled
    broker.get_order.return_value = _filled_order("clav-cycle-1-AAPL-buy", "AAPL", "buy", 8)
    with session_scope(session_factory) as session:
        engine = ExecutionEngine(broker, Repositories(session), clock=FakeClock(NOW))
        engine.reconcile()
        # the "same" decision fires again post-restart (e.g. retried orchestration)
        engine.execute(_decision(), _approved())

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        row = repos.orders.get_by_client_order_id("clav-cycle-1-AAPL-buy")
        assert row is not None
        count = session.query(type(row)).count()
        assert count == 1
    broker.submit_order.assert_called_once()
