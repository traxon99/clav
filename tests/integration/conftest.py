"""Shared fixtures/helpers for scan-cycle-driving integration tests
(Story 1.13 / 1.15)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clav.clock import Clock
from clav.domain.models import Candle, MarketClock, Quote, Timeframe
from clav.interfaces.market_data import MarketDataSource

NOON_UTC = datetime(2025, 6, 2, 16, 0, tzinfo=UTC)  # 12:00 America/New_York


class FakeMarketDataSource(MarketDataSource):
    def __init__(
        self,
        candles_by_symbol: dict[str, list[Candle]],
        *,
        clock: Clock,
        market_open: bool = True,
        fail_symbols: frozenset[str] = frozenset(),
    ) -> None:
        self._candles_by_symbol = candles_by_symbol
        self._clock = clock
        self._market_open = market_open
        self._fail_symbols = fail_symbols
        self.calls: list[str] = []

    def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError("scan cycle doesn't use get_quote in Epic 1")

    def get_candles(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Candle]:
        self.calls.append(symbol)
        if symbol in self._fail_symbols:
            raise ConnectionError(f"simulated data outage for {symbol}")
        return self._candles_by_symbol.get(symbol, [])[-limit:]

    def get_clock(self) -> MarketClock:
        now = self._clock.now()
        return MarketClock(timestamp=now, is_open=self._market_open, next_open=now, next_close=now)


def trending_candles(symbol: str, *, n: int = 60, start_price: float = 100.0) -> list[Candle]:
    """Strongly, monotonically uptrending closes -> guarantees technical_score
    well above the default buy threshold once enough history accumulates."""
    candles = []
    price = start_price
    for i in range(n):
        price += 2.0
        candles.append(
            Candle(
                symbol=symbol,
                timeframe="1Day",
                open=price - 1,
                high=price + 1,
                low=price - 2,
                close=price,
                volume=1000,
                ts=NOON_UTC - timedelta(days=n - i),
            )
        )
    return candles


def flat_candles(symbol: str, *, n: int = 60, price: float = 100.0) -> list[Candle]:
    """A perfectly flat series -> neutral technical_score (HOLD)."""
    return [
        Candle(
            symbol=symbol,
            timeframe="1Day",
            open=price,
            high=price + 0.5,
            low=price - 0.5,
            close=price,
            volume=1000,
            ts=NOON_UTC - timedelta(days=n - i),
        )
        for i in range(n)
    ]
