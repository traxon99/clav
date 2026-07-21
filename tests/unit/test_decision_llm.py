"""Story 3.6 — llm_signal scoring: monotonicity + w_llm=0 regression guard."""

from __future__ import annotations

from datetime import UTC, datetime

from clav.clock import FakeClock
from clav.domain.decision import DecisionEngine, Thresholds, Weights
from clav.domain.models import IndicatorSet, PortfolioSnapshot

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _iset(technical_score: float = 0.0) -> IndicatorSet:
    return IndicatorSet(symbol="AAPL", ts=NOW, close=100.0, technical_score=technical_score)


def _engine(w_llm: float) -> DecisionEngine:
    return DecisionEngine(
        Weights(technical=1.0 - w_llm, llm=w_llm, portfolio=0.0),
        Thresholds(buy=0.2, sell=-0.2),
        default_order_value=1000.0,
        clock=FakeClock(NOW),
    )


def _empty_portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(ts=NOW, cash=10_000, equity=10_000, buying_power=10_000)


def test_llm_signal_shifts_score_monotonically() -> None:
    engine = _engine(w_llm=0.5)
    scores = [
        engine.decide("c", _iset(0.0), llm_signal=s, portfolio=_empty_portfolio()).raw_score
        for s in (-1.0, -0.5, 0.0, 0.5, 1.0)
    ]
    assert scores == sorted(scores)
    assert scores[0] < scores[-1]


def test_w_llm_zero_is_regression_identical_regardless_of_llm_signal() -> None:
    engine = _engine(w_llm=0.0)
    baseline = engine.decide(
        "c", _iset(0.3), llm_signal=0.0, portfolio=_empty_portfolio()
    ).raw_score
    for s in (-1.0, 0.5, 1.0):
        score = engine.decide(
            "c", _iset(0.3), llm_signal=s, portfolio=_empty_portfolio()
        ).raw_score
        assert score == baseline


def test_positive_llm_signal_can_flip_hold_to_buy() -> None:
    engine = _engine(w_llm=0.5)
    hold = engine.decide("c", _iset(0.0), llm_signal=0.0, portfolio=_empty_portfolio())
    buy = engine.decide("c", _iset(0.0), llm_signal=1.0, portfolio=_empty_portfolio())
    assert hold.action == "HOLD"
    assert buy.action == "BUY"
