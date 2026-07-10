"""Pure Pydantic domain types. No vendor imports here — see docs/05-class-design.md §3.

These are the types that flow between domain logic, interfaces, and services.
Persistence (SQLAlchemy) row shapes live separately in ``clav.data.tables``;
repositories are responsible for converting between the two.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Action = Literal["BUY", "SELL", "HOLD"]
OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
TimeInForce = Literal["day", "gtc"]
OrderStatus = Literal[
    "new", "accepted", "partially_filled", "filled", "canceled", "rejected", "failed"
]
Timeframe = Literal["1Min", "5Min", "15Min", "1Hour", "1Day"]


class Quote(BaseModel):
    symbol: str
    price: float
    bid: float | None = None
    ask: float | None = None
    volume: int | None = None
    ts: datetime
    is_stale: bool = False


class Candle(BaseModel):
    symbol: str
    timeframe: Timeframe
    open: float
    high: float
    low: float
    close: float
    volume: int
    ts: datetime
    is_stale: bool = False


class IndicatorSet(BaseModel):
    """A snapshot of technical indicators for one symbol at one point in time.

    Individual indicators are ``None`` when there isn't enough candle history to
    compute them yet — callers must handle partial data rather than crash
    (docs/epics/epic-01-foundation.md, Story 1.8).
    """

    symbol: str
    ts: datetime
    close: float
    sma_20: float | None = None
    ema_50: float | None = None
    rsi_14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    atr_14: float | None = None
    bb_upper: float | None = None
    bb_lower: float | None = None
    vol_avg_20: float | None = None
    technical_score: float | None = None


class TradeDecision(BaseModel):
    cycle_id: str
    symbol: str
    action: Action
    target_qty: int
    raw_score: float
    technical_score: float
    llm_signal: float
    portfolio_bias: float
    reasoning: dict[str, Any] = Field(default_factory=dict)


class RuleOutcome(BaseModel):
    """A single RiskRule's verdict. A rule may veto (passed=False) or cap qty."""

    rule_name: str
    passed: bool
    max_qty: int | None = None
    reason: str = ""


class RiskDecision(BaseModel):
    approved: bool
    adjusted_qty: int
    blocked_by: list[str] = Field(default_factory=list)
    notes: dict[str, Any] = Field(default_factory=dict)


class OrderRequest(BaseModel):
    client_order_id: str
    symbol: str
    side: OrderSide
    qty: int
    order_type: OrderType = "market"
    limit_price: float | None = None
    time_in_force: TimeInForce = "day"


class Order(BaseModel):
    client_order_id: str
    broker_order_id: str | None = None
    symbol: str
    side: OrderSide
    qty: int
    order_type: OrderType = "market"
    limit_price: float | None = None
    status: OrderStatus = "new"
    submitted_at: datetime | None = None
    updated_at: datetime | None = None
    error: str | None = None


class Fill(BaseModel):
    client_order_id: str
    qty: int
    price: float
    fee: float = 0.0
    filled_at: datetime
    broker_fill_id: str | None = None


class Position(BaseModel):
    symbol: str
    qty: int
    avg_entry_price: float
    market_value: float | None = None
    unrealized_pl: float | None = None
    stop_price: float | None = None
    take_profit_price: float | None = None


class Account(BaseModel):
    cash: float
    buying_power: float
    equity: float
    portfolio_value: float


class MarketClock(BaseModel):
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


class PortfolioSnapshot(BaseModel):
    ts: datetime
    cash: float
    equity: float
    buying_power: float
    positions: list[Position] = Field(default_factory=list)
    unrealized_pl: float = 0.0
    realized_pl: float = 0.0
    gross_exposure: float = 0.0
    net_exposure: float = 0.0
    drawdown: float = 0.0
    peak_equity: float = 0.0
    reconciled: bool = True
