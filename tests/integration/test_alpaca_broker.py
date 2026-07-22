"""AlpacaBroker tests against a mocked alpaca-py TradingClient (Story 6.1/6.2).

Mirrors test_paper_broker.py: the mapping logic is shared via
AlpacaBrokerBase (epic-06 decision #2), so these prove the live broker gets
the exact same behavior, not a forked copy. No live Alpaca account is
available in this sandbox — TradingClient is mocked, as in PaperBroker's
suite. See docs/11-testing.md §1.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetClass,
    AssetExchange,
    OrderClass,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)
from alpaca.trading.models import Clock as AlpacaClock
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.models import TradeAccount

from clav.domain.models import OrderRequest
from clav.integrations.alpaca_broker import AlpacaBroker

NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


def _alpaca_order(
    *,
    client_order_id: str = "clav-c1-AAPL-buy",
    symbol: str = "AAPL",
    qty: str = "8",
    side: OrderSide = OrderSide.BUY,
    status: OrderStatus = OrderStatus.FILLED,
    order_id: uuid.UUID | None = None,
) -> AlpacaOrder:
    return AlpacaOrder(
        id=order_id or uuid.uuid4(),
        client_order_id=client_order_id,
        created_at=NOW,
        updated_at=NOW,
        submitted_at=NOW,
        order_class=OrderClass.SIMPLE,
        time_in_force=TimeInForce.DAY,
        status=status,
        extended_hours=False,
        symbol=symbol,
        qty=qty,
        side=side,
        type=OrderType.MARKET,
        order_type=OrderType.MARKET,
    )


def _alpaca_account() -> TradeAccount:
    return TradeAccount(
        id=uuid.uuid4(),
        account_number="LA123",
        status="ACTIVE",
        cash="1000",
        buying_power="2000",
        equity="3000",
        portfolio_value="3000",
    )


def _alpaca_position() -> AlpacaPosition:
    return AlpacaPosition(
        asset_id=uuid.uuid4(),
        symbol="AAPL",
        exchange=AssetExchange.NASDAQ,
        asset_class=AssetClass.US_EQUITY,
        avg_entry_price="190.5",
        qty="8",
        side=PositionSide.LONG,
        cost_basis="1524",
        market_value="1600",
        unrealized_pl="76",
    )


def _alpaca_clock(is_open: bool = True) -> AlpacaClock:
    return AlpacaClock(timestamp=NOW, is_open=is_open, next_open=NOW, next_close=NOW)


def _broker() -> tuple[AlpacaBroker, MagicMock]:
    mock_client = MagicMock(spec=TradingClient)
    broker = AlpacaBroker("key", "secret", client=mock_client)
    return broker, mock_client


def _api_error(status_code: int, code: int = 42210000, message: str = "error") -> APIError:
    http_error = MagicMock()
    http_error.response.status_code = status_code
    return APIError(f'{{"code": {code}, "message": "{message}"}}', http_error)


def test_default_client_is_constructed_with_paper_false(monkeypatch) -> None:
    captured: dict = {}

    class _FakeTradingClient:
        def __init__(self, key: str, secret: str, *, paper: bool) -> None:
            captured["key"] = key
            captured["secret"] = secret
            captured["paper"] = paper

    monkeypatch.setattr("clav.integrations.alpaca_broker.TradingClient", _FakeTradingClient)

    AlpacaBroker("live-key", "live-secret")

    assert captured == {"key": "live-key", "secret": "live-secret", "paper": False}


def test_submit_order_maps_accepted_response() -> None:
    broker, client = _broker()
    client.submit_order.return_value = _alpaca_order(status=OrderStatus.FILLED, qty="8")

    order = broker.submit_order(
        OrderRequest(client_order_id="clav-c1-AAPL-buy", symbol="AAPL", side="buy", qty=8)
    )

    assert order.client_order_id == "clav-c1-AAPL-buy"
    assert order.symbol == "AAPL"
    assert order.side == "buy"
    assert order.qty == 8
    assert order.status == "filled"
    assert order.broker_order_id is not None
    client.submit_order.assert_called_once()


def test_submit_order_rejected_raises() -> None:
    broker, client = _broker()
    client.submit_order.side_effect = _api_error(403, message="insufficient buying power")

    with pytest.raises(APIError):
        broker.submit_order(
            OrderRequest(client_order_id="clav-c1-AAPL-buy", symbol="AAPL", side="buy", qty=8)
        )


def test_submit_order_duplicate_client_order_id_returns_existing_order() -> None:
    broker, client = _broker()
    client.submit_order.side_effect = _api_error(422, message="client_order_id must be unique")
    client.get_order_by_client_id.return_value = _alpaca_order(status=OrderStatus.FILLED)

    order = broker.submit_order(
        OrderRequest(client_order_id="clav-c1-AAPL-buy", symbol="AAPL", side="buy", qty=8)
    )

    assert order.status == "filled"
    client.get_order_by_client_id.assert_called_once_with("clav-c1-AAPL-buy")


def test_get_order_not_found_returns_none() -> None:
    broker, client = _broker()
    client.get_order_by_client_id.side_effect = _api_error(404, message="order not found")

    assert broker.get_order("does-not-exist") is None


def test_get_order_found_maps_status() -> None:
    broker, client = _broker()
    client.get_order_by_client_id.return_value = _alpaca_order(status=OrderStatus.PARTIALLY_FILLED)

    order = broker.get_order("clav-c1-AAPL-buy")
    assert order is not None
    assert order.status == "partially_filled"


def test_cancel_order_calls_broker_with_broker_order_id() -> None:
    broker, client = _broker()
    order_id = uuid.uuid4()
    client.get_order_by_client_id.return_value = _alpaca_order(
        order_id=order_id, status=OrderStatus.NEW
    )

    broker.cancel_order("clav-c1-AAPL-buy")

    client.cancel_order_by_id.assert_called_once_with(str(order_id))


def test_cancel_order_is_noop_when_unknown() -> None:
    broker, client = _broker()
    client.get_order_by_client_id.side_effect = _api_error(404, message="not found")

    broker.cancel_order("does-not-exist")  # should not raise
    client.cancel_order_by_id.assert_not_called()


def test_get_positions_maps_all() -> None:
    broker, client = _broker()
    client.get_all_positions.return_value = [_alpaca_position()]

    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].qty == 8


def test_get_account_maps_fields() -> None:
    broker, client = _broker()
    client.get_account.return_value = _alpaca_account()

    account = broker.get_account()
    assert account.cash == 1000.0
    assert account.buying_power == 2000.0
    assert account.equity == 3000.0


def test_get_clock_maps_fields() -> None:
    broker, client = _broker()
    client.get_clock.return_value = _alpaca_clock(is_open=True)

    clock = broker.get_clock()
    assert clock.is_open is True
