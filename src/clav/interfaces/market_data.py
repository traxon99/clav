"""MarketDataSource — the only way domain/services code reads prices. Concrete
adapters (Alpaca, ...) live in ``clav.integrations`` and implement this ABC.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from clav.domain.models import Candle, MarketClock, Quote, Timeframe


class MarketDataSource(ABC):
    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        """Latest quote for ``symbol``."""

    @abstractmethod
    def get_candles(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Candle]:
        """Last ``limit`` candles for ``symbol`` at ``timeframe``, oldest first."""

    @abstractmethod
    def get_clock(self) -> MarketClock:
        """Current market clock (open/closed, next open/close)."""
