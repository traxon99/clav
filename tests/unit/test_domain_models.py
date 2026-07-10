from datetime import UTC, datetime

from clav.domain.models import (
    Account,
    Candle,
    Fill,
    IndicatorSet,
    MarketClock,
    Order,
    OrderRequest,
    PortfolioSnapshot,
    Position,
    Quote,
    RiskDecision,
    RuleOutcome,
    TradeDecision,
)

NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


def test_quote_roundtrip() -> None:
    q = Quote(symbol="AAPL", price=190.5, bid=190.4, ask=190.6, volume=1000, ts=NOW)
    assert q.is_stale is False
    assert q.model_dump()["symbol"] == "AAPL"


def test_candle_requires_ohlcv() -> None:
    c = Candle(
        symbol="AAPL", timeframe="1Day", open=1, high=2, low=0.5, close=1.5, volume=100, ts=NOW
    )
    assert c.close == 1.5


def test_indicator_set_allows_partial_data() -> None:
    iset = IndicatorSet(symbol="AAPL", ts=NOW, close=190.0)
    assert iset.rsi_14 is None
    assert iset.technical_score is None


def test_trade_decision_defaults_reasoning_to_empty_dict() -> None:
    d = TradeDecision(
        cycle_id="c1",
        symbol="AAPL",
        action="BUY",
        target_qty=10,
        raw_score=0.5,
        technical_score=0.5,
        llm_signal=0.0,
        portfolio_bias=0.0,
    )
    assert d.reasoning == {}


def test_rule_outcome_veto_vs_cap() -> None:
    veto = RuleOutcome(rule_name="EmergencyStopRule", passed=False, reason="estop set")
    cap = RuleOutcome(rule_name="MaxPositionSizeRule", passed=True, max_qty=5, reason="capped")
    assert veto.max_qty is None
    assert cap.max_qty == 5


def test_risk_decision_defaults() -> None:
    rd = RiskDecision(approved=True, adjusted_qty=8)
    assert rd.blocked_by == []
    assert rd.notes == {}


def test_order_request_and_order() -> None:
    req = OrderRequest(client_order_id="clav-c1-AAPL-buy", symbol="AAPL", side="buy", qty=8)
    order = Order(client_order_id=req.client_order_id, symbol="AAPL", side="buy", qty=8)
    assert order.status == "new"
    assert order.client_order_id == req.client_order_id


def test_fill() -> None:
    f = Fill(client_order_id="clav-c1-AAPL-buy", qty=8, price=190.5, filled_at=NOW)
    assert f.fee == 0.0


def test_position_and_account_and_clock() -> None:
    pos = Position(symbol="AAPL", qty=8, avg_entry_price=190.5)
    acct = Account(cash=1000, buying_power=2000, equity=3000, portfolio_value=3000)
    clock = MarketClock(timestamp=NOW, is_open=True, next_open=NOW, next_close=NOW)
    assert pos.stop_price is None
    assert acct.equity == 3000
    assert clock.is_open is True


def test_portfolio_snapshot_defaults() -> None:
    snap = PortfolioSnapshot(ts=NOW, cash=1000, equity=1000, buying_power=1000)
    assert snap.positions == []
    assert snap.reconciled is True
