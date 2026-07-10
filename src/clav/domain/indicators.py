"""Technical indicators, hand-rolled in numpy (no pandas/TA-Lib) per the RAM note
in docs/08-project-structure.md §3 — cheap and exact on a 2 GB Raspberry Pi.

Every indicator function returns ``None`` (rather than raising) when there isn't
enough candle history yet; ``IndicatorService.compute`` never crashes a scan cycle
on short history, it just returns a partially-filled ``IndicatorSet``.
"""

from __future__ import annotations

from typing import TypeAlias

import numpy as np

from clav.domain.models import Candle, IndicatorSet

FloatArray: TypeAlias = "np.ndarray[tuple[int, ...], np.dtype[np.float64]]"

SMA_PERIOD = 20
EMA_PERIOD = 50
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ATR_PERIOD = 14
BOLLINGER_PERIOD = 20
BOLLINGER_STD = 2.0
VOLUME_MA_PERIOD = 20


def sma(values: FloatArray, period: int) -> float | None:
    if len(values) < period:
        return None
    return float(np.mean(values[-period:]))


def _ema_series(values: FloatArray, period: int) -> FloatArray | None:
    """EMA seeded with the SMA of the first `period` values (standard convention)."""
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1)
    out = np.empty(len(values) - period + 1)
    out[0] = values[:period].mean()
    for i, v in enumerate(values[period:], start=1):
        out[i] = (v - out[i - 1]) * alpha + out[i - 1]
    return out


def ema(values: FloatArray, period: int) -> float | None:
    series = _ema_series(values, period)
    return float(series[-1]) if series is not None else None


def rsi(values: FloatArray, period: int = RSI_PERIOD) -> float | None:
    """Wilder's RSI."""
    if len(values) < period + 1:
        return None
    deltas = np.diff(values)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = float(gains[:period].mean())
    avg_loss = float(losses[:period].mean())
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def macd(
    values: FloatArray,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> tuple[float | None, float | None]:
    if len(values) < slow + signal:
        return None, None

    fast_series = _ema_series(values, fast)
    slow_series = _ema_series(values, slow)
    if fast_series is None or slow_series is None:
        return None, None

    n = min(len(fast_series), len(slow_series))
    macd_line = fast_series[-n:] - slow_series[-n:]
    signal_series = _ema_series(macd_line, signal)
    if signal_series is None:
        return float(macd_line[-1]), None
    return float(macd_line[-1]), float(signal_series[-1])


def atr(
    high: FloatArray, low: FloatArray, close: FloatArray, period: int = ATR_PERIOD
) -> float | None:
    """Wilder's Average True Range."""
    if len(close) < period + 1:
        return None
    prev_close = close[:-1]
    true_range = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)),
    )
    atr_val = float(true_range[:period].mean())
    for i in range(period, len(true_range)):
        atr_val = (atr_val * (period - 1) + true_range[i]) / period
    return atr_val


def bollinger_bands(
    values: FloatArray, period: int = BOLLINGER_PERIOD, num_std: float = BOLLINGER_STD
) -> tuple[float | None, float | None]:
    if len(values) < period:
        return None, None
    window = values[-period:]
    mean = float(window.mean())
    std = float(window.std(ddof=0))
    return mean + num_std * std, mean - num_std * std


def volume_ma(volumes: FloatArray, period: int = VOLUME_MA_PERIOD) -> float | None:
    return sma(volumes.astype(float), period)


def _clip(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def technical_score(iset: IndicatorSet) -> float:
    """Blend available indicators into a single deterministic score in [-1, 1].

    All components are trend-following (positive = bullish momentum), so they
    combine consistently: price vs SMA20, MACD histogram, RSI vs midline (50),
    and position within the Bollinger bands. Missing components are skipped;
    if none are available the score is neutral (0.0) — same "fail closed to
    conservative" convention used for llm_signal elsewhere in the system.
    """
    components: list[float] = []

    if iset.sma_20 is not None and iset.sma_20 != 0:
        components.append(_clip((iset.close - iset.sma_20) / iset.sma_20 * 10))

    if iset.macd is not None and iset.macd_signal is not None and iset.close != 0:
        components.append(_clip((iset.macd - iset.macd_signal) / iset.close * 100))

    if iset.rsi_14 is not None:
        components.append(_clip((iset.rsi_14 - 50) / 50))

    if iset.bb_upper is not None and iset.bb_lower is not None:
        bb_mid = (iset.bb_upper + iset.bb_lower) / 2
        half_width = iset.bb_upper - bb_mid
        if half_width != 0:
            components.append(_clip((iset.close - bb_mid) / half_width))

    if not components:
        return 0.0
    return _clip(sum(components) / len(components))


class IndicatorService:
    def compute(self, candles: list[Candle]) -> IndicatorSet:
        """``candles`` must be ordered oldest-first (as CandleRepository.get_recent
        returns them). Returns partial indicators (None) rather than raising when
        history is short."""
        if not candles:
            raise ValueError("compute() requires at least one candle")

        symbol = candles[-1].symbol
        ts = candles[-1].ts
        close_arr = np.array([c.close for c in candles], dtype=float)
        high_arr = np.array([c.high for c in candles], dtype=float)
        low_arr = np.array([c.low for c in candles], dtype=float)
        volume_arr = np.array([c.volume for c in candles], dtype=float)

        macd_val, macd_signal_val = macd(close_arr)
        bb_upper, bb_lower = bollinger_bands(close_arr)

        iset = IndicatorSet(
            symbol=symbol,
            ts=ts,
            close=float(close_arr[-1]),
            sma_20=sma(close_arr, SMA_PERIOD),
            ema_50=ema(close_arr, EMA_PERIOD),
            rsi_14=rsi(close_arr, RSI_PERIOD),
            macd=macd_val,
            macd_signal=macd_signal_val,
            atr_14=atr(high_arr, low_arr, close_arr, ATR_PERIOD),
            bb_upper=bb_upper,
            bb_lower=bb_lower,
            vol_avg_20=volume_ma(volume_arr, VOLUME_MA_PERIOD),
        )
        return iset.model_copy(update={"technical_score": technical_score(iset)})
