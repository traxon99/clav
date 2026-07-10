"""Broker — the only way orders reach the outside world. PaperBroker/DryRunBroker
(Story 1.6) implement this ABC; a live AlpacaBroker adapter is Epic 6.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from clav.domain.models import Account, MarketClock, Order, OrderRequest, Position


class Broker(ABC):
    @abstractmethod
    def submit_order(self, request: OrderRequest) -> Order:
        """Submit an order. Must be idempotent on ``request.client_order_id``."""

    @abstractmethod
    def cancel_order(self, client_order_id: str) -> None:
        """Cancel an open order. No-op if already terminal."""

    @abstractmethod
    def get_order(self, client_order_id: str) -> Order | None:
        """Look up an order by client_order_id, or None if unknown to the broker."""

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """All currently held positions, as reported by the broker."""

    @abstractmethod
    def get_account(self) -> Account:
        """Cash/buying-power/equity, as reported by the broker."""

    @abstractmethod
    def get_clock(self) -> MarketClock:
        """Current market clock (open/closed, next open/close)."""
