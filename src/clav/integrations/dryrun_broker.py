"""DryRunBroker — logs intended orders and sends nothing. Used in tests, the
scan-cycle integration test (Story 1.13), and operator shadow runs."""

from __future__ import annotations

from datetime import timedelta

from clav.clock import Clock
from clav.common.logging import get_logger
from clav.domain.models import Account, MarketClock, Order, OrderRequest, Position
from clav.interfaces.broker import Broker

_DEFAULT_ACCOUNT = Account(
    cash=100_000.0, buying_power=100_000.0, equity=100_000.0, portfolio_value=100_000.0
)


class DryRunBroker(Broker):
    def __init__(
        self,
        *,
        clock: Clock,
        account: Account | None = None,
        positions: list[Position] | None = None,
        market_open: bool = True,
    ) -> None:
        self._clock = clock
        self._account = account or _DEFAULT_ACCOUNT
        self._positions = list(positions or [])
        self._market_open = market_open
        self._logger = get_logger(__name__)
        self.submitted_orders: list[OrderRequest] = []
        self._orders_by_client_id: dict[str, Order] = {}

    def submit_order(self, request: OrderRequest) -> Order:
        existing = self._orders_by_client_id.get(request.client_order_id)
        if existing is not None:
            return existing  # idempotent no-op, matches real-broker semantics

        self._logger.info(
            "dryrun_order_intent",
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            order_type=request.order_type,
            client_order_id=request.client_order_id,
        )
        self.submitted_orders.append(request)
        order = Order(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            order_type=request.order_type,
            limit_price=request.limit_price,
            status="accepted",
            submitted_at=self._clock.now(),
        )
        self._orders_by_client_id[request.client_order_id] = order
        return order

    def cancel_order(self, client_order_id: str) -> None:
        self._logger.info("dryrun_cancel_intent", client_order_id=client_order_id)
        order = self._orders_by_client_id.get(client_order_id)
        if order is not None:
            self._orders_by_client_id[client_order_id] = order.model_copy(
                update={"status": "canceled"}
            )

    def get_order(self, client_order_id: str) -> Order | None:
        return self._orders_by_client_id.get(client_order_id)

    def get_positions(self) -> list[Position]:
        return list(self._positions)

    def get_account(self) -> Account:
        return self._account

    def get_clock(self) -> MarketClock:
        now = self._clock.now()
        return MarketClock(
            timestamp=now,
            is_open=self._market_open,
            next_open=now,
            next_close=now + timedelta(hours=6),
        )
