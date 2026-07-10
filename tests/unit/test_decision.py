from datetime import UTC, datetime

import pytest

from clav.clock import FakeClock
from clav.domain.decision import DecisionEngine, Thresholds, Weights
from clav.domain.models import IndicatorSet, PortfolioSnapshot, Position

NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
WEIGHTS = Weights(technical=1.0, llm=0.0, portfolio=0.0)
THRESHOLDS = Thresholds(buy=0.2, sell=-0.2)


def _engine(default_order_value: float = 1000.0) -> DecisionEngine:
    return DecisionEngine(
        WEIGHTS, THRESHOLDS, default_order_value=default_order_value, clock=FakeClock(NOW)
    )


def _iset(
    technical_score: float | None, close: float = 100.0, symbol: str = "AAPL"
) -> IndicatorSet:
    return IndicatorSet(symbol=symbol, ts=NOW, close=close, technical_score=technical_score)


def _portfolio(positions: list[Position] | None = None) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        ts=NOW, cash=10_000, equity=10_000, buying_power=10_000, positions=positions or []
    )


HOLDING_AAPL = _portfolio([Position(symbol="AAPL", qty=10, avg_entry_price=90.0)])
FLAT = _portfolio()


@pytest.mark.parametrize(
    ("technical_score", "portfolio", "expected_action"),
    [
        (0.5, FLAT, "BUY"),  # above buy threshold, no position
        (0.5, HOLDING_AAPL, "HOLD"),  # already holding: no pyramiding in Epic 1
        (-0.5, HOLDING_AAPL, "SELL"),  # below sell threshold, holding -> exit
        (-0.5, FLAT, "HOLD"),  # below sell threshold but nothing to sell
        (0.0, FLAT, "HOLD"),  # neutral, between thresholds
        (0.2, FLAT, "HOLD"),  # exactly at buy threshold: not strictly greater
        (-0.2, HOLDING_AAPL, "HOLD"),  # exactly at sell threshold: not strictly less
        (0.21, FLAT, "BUY"),  # just above buy threshold
        (-0.21, HOLDING_AAPL, "SELL"),  # just below sell threshold
    ],
)
def test_action_table(technical_score, portfolio, expected_action) -> None:
    decision = _engine().decide("cycle-1", _iset(technical_score), 0.0, portfolio)
    assert decision.action == expected_action


def test_missing_technical_score_treated_as_neutral() -> None:
    decision = _engine().decide("cycle-1", _iset(None), 0.0, FLAT)
    assert decision.action == "HOLD"
    assert decision.technical_score == 0.0


def test_llm_signal_ignored_when_weight_zero_but_still_clamped_and_reported() -> None:
    decision = _engine().decide("cycle-1", _iset(0.0), llm_signal=5.0, portfolio=FLAT)
    assert decision.llm_signal == 1.0  # clamped to [-1, 1]
    assert decision.raw_score == 0.0  # w_llm=0 in Epic 1 -> no influence on score
    assert decision.action == "HOLD"


def test_buy_target_qty_is_flat_notional_over_price() -> None:
    decision = _engine(default_order_value=1000.0).decide(
        "cycle-1", _iset(0.5, close=100.0), 0.0, FLAT
    )
    assert decision.action == "BUY"
    assert decision.target_qty == 10


def test_buy_downgrades_to_hold_when_price_too_high_for_default_notional() -> None:
    decision = _engine(default_order_value=1000.0).decide(
        "cycle-1", _iset(0.9, close=5000.0), 0.0, FLAT
    )
    assert decision.action == "HOLD"
    assert decision.target_qty == 0


def test_sell_target_qty_is_full_exit() -> None:
    decision = _engine().decide("cycle-1", _iset(-0.5, close=100.0), 0.0, HOLDING_AAPL)
    assert decision.action == "SELL"
    assert decision.target_qty == 10


def test_hold_target_qty_is_zero() -> None:
    decision = _engine().decide("cycle-1", _iset(0.0), 0.0, FLAT)
    assert decision.target_qty == 0


def test_portfolio_bias_stubbed_at_zero_in_epic_1() -> None:
    decision = _engine().decide("cycle-1", _iset(0.5), 0.0, FLAT)
    assert decision.portfolio_bias == 0.0


def test_reasoning_contains_score_components() -> None:
    decision = _engine().decide("cycle-1", _iset(0.5), 0.0, FLAT)
    r = decision.reasoning
    assert r["technical_score"] == 0.5
    assert r["raw_score"] == 0.5
    assert r["weights"] == {"technical": 1.0, "llm": 0.0, "portfolio": 0.0}
    assert r["thresholds"] == {"buy": 0.2, "sell": -0.2}
    assert r["holding"] is False


def test_decide_is_deterministic_pure_function() -> None:
    engine = _engine()
    iset = _iset(0.5)
    d1 = engine.decide("cycle-1", iset, 0.0, FLAT)
    d2 = engine.decide("cycle-1", iset, 0.0, FLAT)
    assert d1 == d2


def test_decide_uses_injected_clock_not_wallclock() -> None:
    clock = FakeClock(datetime(2030, 1, 1, tzinfo=UTC))
    engine = DecisionEngine(WEIGHTS, THRESHOLDS, default_order_value=1000.0, clock=clock)
    decision = engine.decide("cycle-1", _iset(0.5), 0.0, FLAT)
    assert decision.reasoning["evaluated_at"] == "2030-01-01T00:00:00+00:00"
