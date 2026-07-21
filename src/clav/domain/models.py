"""Pure Pydantic domain types. No vendor imports here — see docs/05-class-design.md §3.

These are the types that flow between domain logic, interfaces, and services.
Persistence (SQLAlchemy) row shapes live separately in ``clav.data.tables``;
repositories are responsible for converting between the two.
"""

from __future__ import annotations

import hashlib
import re
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


class EarningsEvent(BaseModel):
    symbol: str
    event_type: str
    scheduled_at: datetime
    confirmed: bool = False
    source: str


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


TradeProposalStatus = Literal["executed", "vetoed", "pending", "approved", "rejected", "expired"]


class TradeProposal(BaseModel):
    """A decision-journal entry (Story 3.7): every non-HOLD decision, whether it
    auto-executed, was risk-vetoed, or (optional approval mode) is awaiting a
    human decision. ``inputs_ref`` links back to the news/social rows that fed
    the ``AnalystSignal`` (provenance, Story 3.12)."""

    id: int | None = None
    decision_id: int
    symbol: str
    side: OrderSide
    proposed_qty: int
    executed_qty: int = 0
    rationale: str = ""
    inputs_ref: dict[str, Any] = Field(default_factory=dict)
    status: TradeProposalStatus
    created_at: datetime
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None


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
    filled_qty: int | None = None
    filled_avg_price: float | None = None


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
    sector_allocation: dict[str, float] = Field(default_factory=dict)
    reconciled: bool = True


_WS = re.compile(r"\s+")


def _normalize_text(text: str) -> str:
    """Lower-case, collapse whitespace — used to build a stable dedup key so the
    same story from two sources (or two cycles) hashes identically."""
    return _WS.sub(" ", text).strip().lower()


class NewsItem(BaseModel):
    """One normalized news/filing item for a symbol (Story 3.1).

    ``id`` is the source-native identifier (RSS ``guid``, EDGAR accession no.,
    NewsAPI url) — stable within a source. ``content_hash`` is the cross-source
    dedup key (Story 3.3): the same headline for the same symbol collapses to
    one row regardless of which adapter produced it.
    """

    id: str
    symbol: str
    headline: str
    body: str = ""
    url: str | None = None
    source: str
    published_at: datetime
    fetched_at: datetime
    is_stale: bool = False

    @property
    def content_hash(self) -> str:
        raw = f"{self.symbol.upper()}|{_normalize_text(self.headline)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


SocialSentiment = Literal["bull", "bear", "neutral"]


class Engagement(BaseModel):
    score: int = 0
    replies: int = 0


class SocialItem(BaseModel):
    """One normalized retail-social post (Story 3.2), pre-filter.

    ``author_reputation`` is a source-normalized reputation proxy (Reddit karma,
    StockTwits follower count, …) used by the Stage-1 reputation floor.
    ``sentiment`` is an optional explicit label (StockTwits Bullish/Bearish);
    Reddit posts arrive ``None`` and are classified deterministically in Stage 1.
    """

    symbol: str
    text: str
    author: str
    author_reputation: float = 0.0
    engagement: Engagement = Field(default_factory=Engagement)
    posted_at: datetime
    source: str
    sentiment: SocialSentiment | None = None

    @property
    def dedup_key(self) -> str:
        """Fuzzy key for near-duplicate copypasta collapse: symbol + normalized,
        de-punctuated first N chars of the text (coordinated posts share it)."""
        norm = re.sub(r"[^a-z0-9 ]", "", _normalize_text(self.text))
        return f"{self.symbol.upper()}|{norm[:120]}"


class SocialDigest(BaseModel):
    """Compact, manipulation-resistant per-symbol social summary (Story 3.2).

    This — not the raw firehose — is what reaches Gemini (Story 3.4). A single
    bot cannot move an aggregate; a real mood shift can.
    """

    symbol: str
    qualifying_post_count: int = 0
    bull_count: int = 0
    bear_count: int = 0
    bull_bear_ratio: float = 1.0
    mention_volume: int = 0
    baseline_volume: float = 0.0
    volume_ratio: float = 1.0
    anomaly_flag: bool = False
    top_posts: list[SocialItem] = Field(default_factory=list)
    generated_at: datetime

    @property
    def is_empty(self) -> bool:
        return self.qualifying_post_count == 0
