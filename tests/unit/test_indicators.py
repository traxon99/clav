from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from clav.domain.indicators import (
    IndicatorService,
    atr,
    bollinger_bands,
    macd,
    rsi,
    sma,
    technical_score,
    volume_ma,
)
from clav.domain.models import Candle, IndicatorSet

BASE_TS = datetime(2025, 1, 1, tzinfo=UTC)


def _candles(closes: list[float], *, highs=None, lows=None, volumes=None) -> list[Candle]:
    highs = highs or [c + 0.5 for c in closes]
    lows = lows or [c - 0.5 for c in closes]
    volumes = volumes or [1000] * len(closes)
    return [
        Candle(
            symbol="AAPL",
            timeframe="1Day",
            open=c,
            high=h,
            low=lo,
            close=c,
            volume=v,
            ts=BASE_TS + timedelta(days=i),
        )
        for i, (c, h, lo, v) in enumerate(zip(closes, highs, lows, volumes, strict=True))
    ]


# --- SMA ---------------------------------------------------------------


def test_sma_reference_value() -> None:
    values = np.array([float(i) for i in range(1, 26)])  # 1..25
    # last 20 values are 6..25 -> mean = 15.5
    assert sma(values, 20) == pytest.approx(15.5)


def test_sma_none_when_insufficient_history() -> None:
    values = np.array([1.0, 2.0, 3.0])
    assert sma(values, 20) is None


# --- RSI -----------------------------------------------------------------


def test_rsi_reference_value_alternating_series() -> None:
    # 15 closes -> 14 deltas alternating +2, -1, +2, -1, ...
    closes = [100.0]
    for _ in range(7):
        closes.append(closes[-1] + 2)
        closes.append(closes[-1] - 1)
    values = np.array(closes)
    # avg_gain = 1.0 (7 gains of 2 / 14), avg_loss = 0.5 (7 losses of 1 / 14)
    # RS = 2.0 -> RSI = 100 - 100/3 = 66.666...
    assert rsi(values, 14) == pytest.approx(200 / 3, abs=1e-9)


def test_rsi_is_100_when_no_losses() -> None:
    values = np.array([float(i) for i in range(1, 16)])  # strictly increasing
    assert rsi(values, 14) == 100.0


def test_rsi_none_when_insufficient_history() -> None:
    assert rsi(np.array([1.0, 2.0]), 14) is None


# --- MACD / EMA on a flat series (exact-zero reference) -------------------


def test_macd_is_zero_for_flat_price_series() -> None:
    values = np.full(40, 100.0)
    macd_val, signal_val = macd(values)
    assert macd_val == pytest.approx(0.0, abs=1e-9)
    assert signal_val == pytest.approx(0.0, abs=1e-9)


def test_macd_none_when_insufficient_history() -> None:
    values = np.full(10, 100.0)
    assert macd(values) == (None, None)


def test_macd_positive_for_uptrend() -> None:
    values = np.array([100.0 + i for i in range(40)])
    macd_val, signal_val = macd(values)
    assert macd_val is not None and macd_val > 0
    assert signal_val is not None


# --- ATR -------------------------------------------------------------------


def test_atr_reference_value_constant_range() -> None:
    # high=105, low=95, close=100 for every bar -> true range is always 10
    n = 15
    high = np.full(n, 105.0)
    low = np.full(n, 95.0)
    close = np.full(n, 100.0)
    assert atr(high, low, close, period=14) == pytest.approx(10.0)


def test_atr_none_when_insufficient_history() -> None:
    high = np.full(5, 105.0)
    low = np.full(5, 95.0)
    close = np.full(5, 100.0)
    assert atr(high, low, close, period=14) is None


# --- Bollinger Bands ---------------------------------------------------


def test_bollinger_bands_reference_value_flat_series() -> None:
    values = np.full(20, 100.0)
    upper, lower = bollinger_bands(values, period=20, num_std=2.0)
    assert upper == pytest.approx(100.0)
    assert lower == pytest.approx(100.0)


def test_bollinger_bands_none_when_insufficient_history() -> None:
    values = np.full(5, 100.0)
    assert bollinger_bands(values, period=20) == (None, None)


# --- Volume MA ---------------------------------------------------------


def test_volume_ma_reference_value() -> None:
    volumes = np.array([float(i) for i in range(1, 21)])  # 1..20
    assert volume_ma(volumes, period=20) == pytest.approx(10.5)


# --- technical_score -----------------------------------------------------


def test_technical_score_neutral_when_no_components_available() -> None:
    iset = IndicatorSet(symbol="AAPL", ts=BASE_TS, close=100.0)
    assert technical_score(iset) == 0.0


def test_technical_score_is_bounded() -> None:
    iset = IndicatorSet(
        symbol="AAPL",
        ts=BASE_TS,
        close=1000.0,
        sma_20=1.0,  # huge relative deviation, should clip to 1.0 not explode
        rsi_14=100.0,
        macd=1000.0,
        macd_signal=0.0,
    )
    score = technical_score(iset)
    assert -1.0 <= score <= 1.0


def test_technical_score_zero_for_flat_neutral_indicators() -> None:
    iset = IndicatorSet(
        symbol="AAPL",
        ts=BASE_TS,
        close=100.0,
        sma_20=100.0,
        rsi_14=50.0,
        macd=0.0,
        macd_signal=0.0,
        bb_upper=105.0,
        bb_lower=95.0,
    )
    assert technical_score(iset) == pytest.approx(0.0, abs=1e-9)


# --- IndicatorService.compute --------------------------------------------


def test_compute_handles_insufficient_history_gracefully() -> None:
    candles = _candles([100.0, 101.0, 102.0])  # far short of any period
    iset = IndicatorService().compute(candles)

    assert iset.symbol == "AAPL"
    assert iset.close == 102.0
    assert iset.sma_20 is None
    assert iset.rsi_14 is None
    assert iset.macd is None
    assert iset.technical_score == 0.0  # neutral default, never crashes


def test_compute_requires_at_least_one_candle() -> None:
    with pytest.raises(ValueError, match="at least one candle"):
        IndicatorService().compute([])


def test_compute_populates_technical_score_when_history_sufficient() -> None:
    candles = _candles([100.0 + i for i in range(60)])
    iset = IndicatorService().compute(candles)

    assert iset.sma_20 is not None
    assert iset.ema_50 is not None
    assert iset.rsi_14 is not None
    assert iset.macd is not None
    assert iset.technical_score is not None
    assert -1.0 <= iset.technical_score <= 1.0
