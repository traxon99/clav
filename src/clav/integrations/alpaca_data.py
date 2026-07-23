"""AlpacaDataAdapter — implements MarketDataSource via alpaca-py's market-data
and trading clients (Story 1.7). Only fetches the last K bars per call (RAM
discipline on the Pi, docs/08-project-structure.md §3).

Persistence note: unlike a design where the adapter owns a DB session itself,
here it stays session-free and purely returns normalized domain objects —
``ScanCycleService`` (Story 1.13) persists what it returns via
``CandleRepository`` within its own unit-of-work. The "serve last-known values
marked stale on failure" behavior from docs/02-modules.md §1 is supported via
an injected ``fallback_candles`` callable (wired to ``CandleRepository.get_recent``
by the composition root) rather than a stored session, keeping this adapter
trivially unit-testable.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models.bars import BarSet
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Clock as AlpacaClock

from clav.clock import Clock
from clav.common.logging import get_logger
from clav.common.retry import retry_transient
from clav.domain.models import Candle, MarketClock, Quote, Timeframe, TradableAsset
from clav.interfaces.market_data import MarketDataSource

_logger = get_logger(__name__)

_TIMEFRAME_MAP: dict[Timeframe, TimeFrame] = {
    "1Min": TimeFrame(1, TimeFrameUnit.Minute),
    "5Min": TimeFrame(5, TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
    "1Day": TimeFrame(1, TimeFrameUnit.Day),
}

_BARS_PER_TRADING_DAY: dict[Timeframe, float] = {
    "1Min": 390,
    "5Min": 78,
    "15Min": 26,
    "1Hour": 6.5,
    "1Day": 1,
}

FallbackCandlesFn = Callable[[str, Timeframe, int], list[Candle]]


def _lookback_start(now: datetime, timeframe: Timeframe, limit: int) -> datetime:
    """A generous calendar-day lookback covering `limit` bars, accounting for
    weekends/holidays, so ``start`` always comfortably reaches back far enough."""
    trading_days_needed = math.ceil(limit / _BARS_PER_TRADING_DAY[timeframe])
    calendar_days = math.ceil(trading_days_needed * 1.6) + 10
    return now - timedelta(days=calendar_days)


_retry = retry_transient()


class AlpacaDataAdapter(MarketDataSource):
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        clock: Clock,
        data_client: StockHistoricalDataClient | None = None,
        trading_client: TradingClient | None = None,
        fallback_candles: FallbackCandlesFn | None = None,
    ) -> None:
        self._data_client = data_client or StockHistoricalDataClient(api_key, api_secret)
        self._trading_client = trading_client or TradingClient(api_key, api_secret, paper=True)
        self._clock = clock
        self._fallback_candles = fallback_candles

    @_retry
    def get_quote(self, symbol: str) -> Quote:
        result = self._data_client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=symbol)
        )
        q = result[symbol]
        price = q.ask_price or q.bid_price
        return Quote(
            symbol=symbol,
            price=float(price),
            bid=float(q.bid_price) if q.bid_price else None,
            ask=float(q.ask_price) if q.ask_price else None,
            ts=q.timestamp,
            is_stale=False,
        )

    def get_candles(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Candle]:
        try:
            return self._fetch_candles(symbol, timeframe, limit)
        except Exception as exc:
            _logger.warning(
                "candle_fetch_failed_falling_back_to_last_known",
                symbol=symbol,
                timeframe=timeframe,
                error=str(exc),
            )
            if self._fallback_candles is None:
                raise
            stale = self._fallback_candles(symbol, timeframe, limit)
            return [c.model_copy(update={"is_stale": True}) for c in stale]

    @_retry
    def _fetch_candles(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Candle]:
        now = self._clock.now()
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=_TIMEFRAME_MAP[timeframe],
            start=_lookback_start(now, timeframe, limit),
            limit=limit,
        )
        barset = self._data_client.get_stock_bars(request)
        if not isinstance(barset, BarSet):
            raise TypeError(f"expected BarSet from alpaca-py, got {type(barset)!r}")
        bars: list[Any] = barset.data.get(symbol, [])
        return [
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=int(b.volume),
                ts=b.timestamp,
            )
            for b in bars[-limit:]
        ]

    @_retry
    def list_assets(self) -> list[TradableAsset]:
        """The active, tradeable US-equity catalog from Alpaca (autonomous-discovery
        epic). One keyed-but-cheap call, refreshed on a slow cadence; never called
        per scan cycle."""
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        assets = self._trading_client.get_all_assets(
            GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
        )
        out: list[TradableAsset] = []
        for a in assets:
            symbol = getattr(a, "symbol", None)
            if not symbol:
                continue
            out.append(
                TradableAsset(
                    symbol=str(symbol),
                    name=getattr(a, "name", None),
                    exchange=str(getattr(a, "exchange", "") or "") or None,
                    tradable=bool(getattr(a, "tradable", True)),
                    fractionable=bool(getattr(a, "fractionable", False)),
                )
            )
        return out

    @_retry
    def get_clock(self) -> MarketClock:
        c = self._trading_client.get_clock()
        if not isinstance(c, AlpacaClock):
            raise TypeError(f"expected Clock from alpaca-py, got {type(c)!r}")
        return MarketClock(
            timestamp=c.timestamp,
            is_open=c.is_open,
            next_open=c.next_open,
            next_close=c.next_close,
        )
