"""AlpacaDataAdapter tests against mocked alpaca-py clients — same rationale as
tests/integration/test_paper_broker.py: no live account/cassettes available,
so we mock StockHistoricalDataClient/TradingClient and return alpaca-py's own
response model instances."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models.bars import BarSet
from alpaca.data.models.quotes import Quote as AlpacaQuote
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Clock as AlpacaClock

from clav.clock import FakeClock
from clav.domain.models import Candle
from clav.integrations.alpaca_data import AlpacaDataAdapter

NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


def _bar(t: str, o: float, h: float, low: float, c: float, v: int) -> dict:
    return {"t": t, "o": o, "h": h, "l": low, "c": c, "v": v, "n": 10, "vw": (h + low) / 2}


def _barset(symbol: str, bars: list[dict]) -> BarSet:
    return BarSet({symbol: bars})


def _quote_response(symbol: str, bid: float, ask: float) -> dict:
    return {
        symbol: AlpacaQuote(
            symbol,
            {
                "t": "2025-06-01T12:00:00Z",
                "ax": "NASDAQ",
                "ap": ask,
                "as": 10,
                "bx": "NASDAQ",
                "bp": bid,
                "bs": 10,
                "c": [],
            },
        )
    }


def _alpaca_clock(is_open: bool = True) -> AlpacaClock:
    return AlpacaClock(timestamp=NOW, is_open=is_open, next_open=NOW, next_close=NOW)


def _adapter(fallback=None) -> tuple[AlpacaDataAdapter, MagicMock, MagicMock]:
    data_client = MagicMock(spec=StockHistoricalDataClient)
    trading_client = MagicMock(spec=TradingClient)
    adapter = AlpacaDataAdapter(
        "key",
        "secret",
        clock=FakeClock(NOW),
        data_client=data_client,
        trading_client=trading_client,
        fallback_candles=fallback,
    )
    return adapter, data_client, trading_client


def _api_error(status_code: int | None, message: str = "error") -> APIError:
    if status_code is None:
        return APIError(f'{{"code": 500, "message": "{message}"}}', None)
    http_error = MagicMock()
    http_error.response.status_code = status_code
    return APIError(f'{{"code": {status_code}, "message": "{message}"}}', http_error)


# --- get_quote -----------------------------------------------------------


def test_get_quote_maps_response() -> None:
    adapter, data_client, _ = _adapter()
    data_client.get_stock_latest_quote.return_value = _quote_response("AAPL", 190.4, 190.6)

    quote = adapter.get_quote("AAPL")

    assert quote.symbol == "AAPL"
    assert quote.bid == 190.4
    assert quote.ask == 190.6
    assert quote.price == 190.6
    assert quote.is_stale is False


def test_get_quote_retries_on_transient_error_then_succeeds() -> None:
    adapter, data_client, _ = _adapter()
    data_client.get_stock_latest_quote.side_effect = [
        _api_error(503),
        _quote_response("AAPL", 190.4, 190.6),
    ]

    quote = adapter.get_quote("AAPL")
    assert quote.price == 190.6
    assert data_client.get_stock_latest_quote.call_count == 2


def test_get_quote_does_not_retry_permanent_error() -> None:
    adapter, data_client, _ = _adapter()
    data_client.get_stock_latest_quote.side_effect = _api_error(404, "unknown symbol")

    with pytest.raises(APIError):
        adapter.get_quote("BOGUS")
    assert data_client.get_stock_latest_quote.call_count == 1


def test_get_quote_gives_up_after_max_retries_on_persistent_timeout() -> None:
    adapter, data_client, _ = _adapter()
    data_client.get_stock_latest_quote.side_effect = requests.exceptions.Timeout("timed out")

    with pytest.raises(requests.exceptions.Timeout):
        adapter.get_quote("AAPL")
    assert data_client.get_stock_latest_quote.call_count == 3


# --- get_candles -----------------------------------------------------------


def test_get_candles_maps_and_normalizes() -> None:
    adapter, data_client, _ = _adapter()
    data_client.get_stock_bars.return_value = _barset(
        "AAPL",
        [
            _bar("2025-01-01T00:00:00Z", 100, 105, 99, 104, 1000),
            _bar("2025-01-02T00:00:00Z", 104, 110, 103, 108, 1200),
        ],
    )

    candles = adapter.get_candles("AAPL", "1Day", limit=2)

    assert len(candles) == 2
    assert candles[0].close == 104.0
    assert candles[1].close == 108.0
    assert all(c.is_stale is False for c in candles)


def test_get_candles_falls_back_to_last_known_and_marks_stale_on_failure() -> None:
    stale_candle = Candle(
        symbol="AAPL", timeframe="1Day", open=1, high=2, low=0.5, close=1.5, volume=100, ts=NOW
    )

    def fallback(symbol, timeframe, limit):
        return [stale_candle]

    adapter, data_client, _ = _adapter(fallback=fallback)
    data_client.get_stock_bars.side_effect = requests.exceptions.ConnectionError("down")

    candles = adapter.get_candles("AAPL", "1Day", limit=1)

    assert len(candles) == 1
    assert candles[0].is_stale is True
    assert candles[0].close == 1.5


def test_get_candles_raises_when_no_fallback_configured() -> None:
    adapter, data_client, _ = _adapter(fallback=None)
    data_client.get_stock_bars.side_effect = requests.exceptions.ConnectionError("down")

    with pytest.raises(requests.exceptions.ConnectionError):
        adapter.get_candles("AAPL", "1Day", limit=1)


def test_get_candles_malformed_response_is_not_retried() -> None:
    adapter, data_client, _ = _adapter(fallback=None)
    data_client.get_stock_bars.side_effect = ValueError("malformed payload")

    with pytest.raises(ValueError, match="malformed payload"):
        adapter.get_candles("AAPL", "1Day", limit=1)
    assert data_client.get_stock_bars.call_count == 1


# --- get_clock -------------------------------------------------------------


def test_get_clock_maps_response() -> None:
    adapter, _, trading_client = _adapter()
    trading_client.get_clock.return_value = _alpaca_clock(is_open=True)

    clock = adapter.get_clock()
    assert clock.is_open is True
