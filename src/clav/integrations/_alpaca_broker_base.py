"""Shared Alpaca order/status/account mapping for PaperBroker and AlpacaBroker
(Story 6.1/6.2, epic-06 decision #2). The two brokers differ only in which
endpoint/key pair the underlying ``TradingClient`` talks to (paper vs. live);
every request-building, status-normalization, and error-handling rule lives
here exactly once, so a live-only order bug can't hide in a forked copy.

Testing note: alpaca-py's TradingClient talks HTTP via ``requests`` (not
httpx), and there's no live account/API key available in this sandbox to
record authentic VCR cassettes against. Tests inject a mocked ``TradingClient``
built from alpaca-py's own response model classes, so the mapping/error-
handling logic here is exercised against exactly-shaped responses without
hitting the network — satisfying "no live network in CI" the same way a
cassette would.
"""

from __future__ import annotations

from typing import Any

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from clav.domain.models import (
    Account,
    MarketClock,
    Order,
    OrderRequest,
    OrderStatus,
    Position,
)
from clav.interfaces.broker import Broker

_STATUS_MAP: dict[str, OrderStatus] = {
    "new": "new",
    "pending_new": "new",
    "accepted": "accepted",
    "pending_replace": "accepted",
    "calculated": "accepted",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "done_for_day": "canceled",
    "canceled": "canceled",
    "expired": "canceled",
    "replaced": "canceled",
    "pending_cancel": "accepted",
    "stopped": "canceled",
    "rejected": "rejected",
    "suspended": "rejected",
}


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _map_status(raw: Any) -> OrderStatus:
    return _STATUS_MAP.get(_enum_value(raw), "accepted")


def _to_domain_order(o: Any) -> Order:
    order_type = "limit" if _enum_value(o.type) == "limit" else "market"
    return Order(
        client_order_id=o.client_order_id,
        broker_order_id=str(o.id) if o.id else None,
        symbol=o.symbol,
        side=_enum_value(o.side),
        qty=int(float(o.qty)) if o.qty is not None else 0,
        order_type=order_type,
        limit_price=float(o.limit_price) if o.limit_price is not None else None,
        status=_map_status(o.status),
        submitted_at=o.submitted_at,
        updated_at=o.updated_at,
        filled_qty=int(float(o.filled_qty)) if o.filled_qty else None,
        filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None,
    )


def _to_domain_position(p: Any) -> Position:
    return Position(
        symbol=p.symbol,
        qty=int(float(p.qty)),
        avg_entry_price=float(p.avg_entry_price),
        market_value=float(p.market_value) if p.market_value is not None else None,
        unrealized_pl=float(p.unrealized_pl) if p.unrealized_pl is not None else None,
    )


def _to_domain_account(a: Any) -> Account:
    return Account(
        cash=float(a.cash),
        buying_power=float(a.buying_power),
        equity=float(a.equity),
        portfolio_value=float(a.portfolio_value),
    )


def _to_domain_clock(c: Any) -> MarketClock:
    return MarketClock(
        timestamp=c.timestamp, is_open=c.is_open, next_open=c.next_open, next_close=c.next_close
    )


def _build_alpaca_request(request: OrderRequest) -> MarketOrderRequest | LimitOrderRequest:
    side = AlpacaOrderSide.BUY if request.side == "buy" else AlpacaOrderSide.SELL
    tif = AlpacaTimeInForce.DAY if request.time_in_force == "day" else AlpacaTimeInForce.GTC
    if request.order_type == "limit":
        return LimitOrderRequest(
            symbol=request.symbol,
            qty=request.qty,
            side=side,
            time_in_force=tif,
            limit_price=request.limit_price,
            client_order_id=request.client_order_id,
        )
    return MarketOrderRequest(
        symbol=request.symbol,
        qty=request.qty,
        side=side,
        time_in_force=tif,
        client_order_id=request.client_order_id,
    )


def _is_duplicate_client_order_id(exc: APIError) -> bool:
    try:
        return exc.status_code == 422 and "client_order_id" in (exc.message or "")
    except Exception:
        return False


class AlpacaBrokerBase(Broker):
    """Implements ``Broker`` purely in terms of ``self._client``; subclasses
    (``PaperBroker``, ``AlpacaBroker``) only choose which ``TradingClient``
    (paper vs. live endpoint, which key pair) to construct."""

    _client: TradingClient

    def submit_order(self, request: OrderRequest) -> Order:
        try:
            result = self._client.submit_order(_build_alpaca_request(request))
        except APIError as exc:
            if _is_duplicate_client_order_id(exc):
                existing = self.get_order(request.client_order_id)
                if existing is not None:
                    return existing
            raise
        return _to_domain_order(result)

    def cancel_order(self, client_order_id: str) -> None:
        existing = self.get_order(client_order_id)
        if existing is None or existing.broker_order_id is None:
            return
        try:
            self._client.cancel_order_by_id(existing.broker_order_id)
        except APIError as exc:
            if exc.status_code == 404:
                return
            raise

    def get_order(self, client_order_id: str) -> Order | None:
        try:
            result = self._client.get_order_by_client_id(client_order_id)
        except APIError as exc:
            if exc.status_code == 404:
                return None
            raise
        return _to_domain_order(result)

    def get_positions(self) -> list[Position]:
        positions = self._client.get_all_positions()
        return [_to_domain_position(p) for p in positions]

    def get_account(self) -> Account:
        return _to_domain_account(self._client.get_account())

    def get_clock(self) -> MarketClock:
        return _to_domain_clock(self._client.get_clock())
