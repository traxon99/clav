"""ExecutionEngine — validates and submits approved orders through the Broker
interface idempotently, and reconciles broker state into the DB before any new
decision (Story 1.11, docs/02-modules.md §6).

Two independent idempotency layers, protecting against different failure
modes:
- ``client_order_id = clav-<cycle_id>-<symbol>-<side>`` is deterministic and
  UNIQUE in the DB (Story 1.4), so re-running ``execute()`` for the same
  decision (retry, bug, restart) never creates a second order.
- ``RiskEngine``'s ``DuplicateOrderRule`` (Story 1.10) separately blocks a
  *new* decision from being approved while a symbol/side already has an open
  order in flight.
"""

from __future__ import annotations

from collections.abc import Callable

from clav.clock import Clock
from clav.common.logging import get_logger
from clav.common.retry import retry_transient
from clav.data.repositories import Repositories
from clav.domain.models import Fill, Order, OrderRequest, OrderSide, RiskDecision, TradeDecision
from clav.interfaces.broker import Broker

AlertHook = Callable[[str, str], None]


class ExecutionEngine:
    def __init__(
        self,
        broker: Broker,
        repos: Repositories,
        *,
        clock: Clock,
        alert_hook: AlertHook | None = None,
    ) -> None:
        self._broker = broker
        self._repos = repos
        self._clock = clock
        self._alert_hook = alert_hook
        self._logger = get_logger(__name__)

    def execute(
        self,
        decision: TradeDecision,
        risk_decision: RiskDecision,
        *,
        decision_id: int | None = None,
    ) -> Order | None:
        if not risk_decision.approved or risk_decision.adjusted_qty <= 0:
            return None
        if decision.action not in ("BUY", "SELL"):
            return None

        side: OrderSide = "buy" if decision.action == "BUY" else "sell"
        client_order_id = f"clav-{decision.cycle_id}-{decision.symbol}-{side}"

        existing_row = self._repos.orders.get_by_client_order_id(client_order_id)
        if existing_row is not None:
            self._logger.info(
                "execution_skipped_duplicate_client_order_id",
                client_order_id=client_order_id,
            )
            return Order(
                client_order_id=existing_row.client_order_id,
                broker_order_id=existing_row.broker_order_id,
                symbol=decision.symbol,
                side=existing_row.side,
                qty=existing_row.qty,
                order_type=existing_row.order_type,
                limit_price=existing_row.limit_price,
                status=existing_row.status,
                submitted_at=existing_row.submitted_at,
                updated_at=existing_row.updated_at,
                error=existing_row.error,
            )

        market_clock = self._broker.get_clock()
        if not market_clock.is_open:
            self._logger.warning("execution_blocked_market_closed", client_order_id=client_order_id)
            return None

        if decision.action == "BUY":
            account = self._broker.get_account()
            if account.buying_power <= 0:
                self._logger.warning(
                    "execution_blocked_no_buying_power", client_order_id=client_order_id
                )
                return None

        instrument = self._repos.instruments.get_or_create(decision.symbol)
        request = OrderRequest(
            client_order_id=client_order_id,
            symbol=decision.symbol,
            side=side,
            qty=risk_decision.adjusted_qty,
        )

        try:
            broker_order = self._submit(request)
        except Exception as exc:
            self._logger.error(
                "execution_failed_permanently", client_order_id=client_order_id, error=str(exc)
            )
            row = self._repos.orders.create(
                instrument_id=instrument.id,
                decision_id=decision_id,
                request=request,
                submitted_at=self._clock.now(),
            )
            row.status = "failed"
            row.error = str(exc)
            self._alert(client_order_id, str(exc))
            return None

        order_row = self._repos.orders.create(
            instrument_id=instrument.id,
            decision_id=decision_id,
            request=request,
            submitted_at=self._clock.now(),
        )
        self._repos.orders.update_from_broker_order(client_order_id, broker_order)
        self._capture_fill_if_present(order_row.id, broker_order)

        return broker_order

    def reconcile(self) -> None:
        """Sync every locally-open order against the broker. Must run before
        any new decision — never assume a locally-open order is still open."""
        open_rows = self._repos.orders.get_open_orders()
        for row in open_rows:
            broker_order = self._broker.get_order(row.client_order_id)
            if broker_order is None:
                row.status = "failed"
                row.error = "order not found on broker during reconciliation"
                row.updated_at = self._clock.now()
                self._logger.warning(
                    "reconcile_order_missing_on_broker", client_order_id=row.client_order_id
                )
                continue

            self._repos.orders.update_from_broker_order(row.client_order_id, broker_order)
            self._capture_fill_if_present(row.id, broker_order)
            self._logger.info(
                "reconciled_order",
                client_order_id=row.client_order_id,
                status=broker_order.status,
            )

    @retry_transient()
    def _submit(self, request: OrderRequest) -> Order:
        return self._broker.submit_order(request)

    def _capture_fill_if_present(self, order_row_id: int, broker_order: Order) -> None:
        if (
            broker_order.status == "filled"
            and broker_order.filled_qty
            and broker_order.filled_avg_price
        ):
            fill = Fill(
                client_order_id=broker_order.client_order_id,
                qty=broker_order.filled_qty,
                price=broker_order.filled_avg_price,
                filled_at=broker_order.updated_at or self._clock.now(),
            )
            self._repos.fills.add(order_row_id, fill)

    def _alert(self, client_order_id: str, message: str) -> None:
        if self._alert_hook is not None:
            self._alert_hook(client_order_id, message)
        else:
            self._logger.critical(
                "execution_alert_no_hook_configured",
                client_order_id=client_order_id,
                message=message,
            )
