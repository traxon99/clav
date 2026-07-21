"""SQLAlchemy ORM models for the Epic-1/Epic-2 table subset (docs/03-database.md §3).

Only tables needed through Epic 2 are defined here (no news_item, analysis_result,
trade_review, health_event, config_snapshot yet — those arrive with the epics
that use them).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    __tablename__ = "instrument"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(128), default=None)
    exchange: Mapped[str | None] = mapped_column(String(16), default=None)
    sector: Mapped[str | None] = mapped_column(String(64), default=None)
    industry: Mapped[str | None] = mapped_column(String(64), default=None)
    asset_class: Mapped[str] = mapped_column(String(16), default="us_equity")
    is_active: Mapped[bool] = mapped_column(default=True)


class EarningsEvent(Base):
    __tablename__ = "earnings_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    scheduled_at: Mapped[datetime] = mapped_column(index=True)
    confirmed: Mapped[bool] = mapped_column(default=False)
    source: Mapped[str] = mapped_column(String(32))


class Candle(Base):
    __tablename__ = "candle"
    __table_args__ = (
        UniqueConstraint("instrument_id", "timeframe", "ts", name="uq_candle_instrument_tf_ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    open: Mapped[float]
    high: Mapped[float]
    low: Mapped[float]
    close: Mapped[float]
    volume: Mapped[int]
    ts: Mapped[datetime] = mapped_column(index=True)


class IndicatorSet(Base):
    __tablename__ = "indicator_set"

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), index=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    close: Mapped[float]
    sma_20: Mapped[float | None] = mapped_column(default=None)
    ema_50: Mapped[float | None] = mapped_column(default=None)
    rsi_14: Mapped[float | None] = mapped_column(default=None)
    macd: Mapped[float | None] = mapped_column(default=None)
    macd_signal: Mapped[float | None] = mapped_column(default=None)
    atr_14: Mapped[float | None] = mapped_column(default=None)
    bb_upper: Mapped[float | None] = mapped_column(default=None)
    bb_lower: Mapped[float | None] = mapped_column(default=None)
    vol_avg_20: Mapped[float | None] = mapped_column(default=None)
    technical_score: Mapped[float | None] = mapped_column(default=None)


class ScanCycle(Base):
    __tablename__ = "scan_cycle"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    mode: Mapped[str] = mapped_column(String(16))
    market_open: Mapped[bool]
    trigger: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="running")


class Decision(Base):
    __tablename__ = "decision"

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_cycle_id: Mapped[str] = mapped_column(ForeignKey("scan_cycle.id"), index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), index=True)
    action: Mapped[str] = mapped_column(String(8))
    raw_score: Mapped[float]
    technical_score: Mapped[float]
    llm_signal: Mapped[float]
    portfolio_bias: Mapped[float]
    target_qty: Mapped[int]
    reasoning: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime]


class RiskEvaluation(Base):
    __tablename__ = "risk_evaluation"

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decision.id"), index=True)
    approved: Mapped[bool]
    adjusted_qty: Mapped[int]
    blocked_by: Mapped[list[str]] = mapped_column(JSON, default=list)
    notes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evaluated_at: Mapped[datetime] = mapped_column(index=True)


class Order(Base):
    __tablename__ = "order"
    __table_args__ = (UniqueConstraint("client_order_id", name="uq_order_client_order_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decision.id"), default=None)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), index=True)
    client_order_id: Mapped[str] = mapped_column(String(64))
    broker_order_id: Mapped[str | None] = mapped_column(String(64), default=None)
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column("type", String(8), default="market")
    qty: Mapped[int]
    limit_price: Mapped[float | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(String(16), default="new")
    submitted_at: Mapped[datetime | None] = mapped_column(default=None)
    updated_at: Mapped[datetime | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(default=None)


class Fill(Base):
    __tablename__ = "fill"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("order.id"), index=True)
    qty: Mapped[int]
    price: Mapped[float]
    fee: Mapped[float] = mapped_column(default=0.0)
    filled_at: Mapped[datetime]
    broker_fill_id: Mapped[str | None] = mapped_column(default=None)


class Trade(Base):
    __tablename__ = "trade"

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), index=True)
    entry_order_id: Mapped[int] = mapped_column(ForeignKey("order.id"))
    exit_order_id: Mapped[int | None] = mapped_column(ForeignKey("order.id"), default=None)
    entry_decision_id: Mapped[int | None] = mapped_column(ForeignKey("decision.id"), default=None)
    qty: Mapped[int]
    entry_price: Mapped[float]
    exit_price: Mapped[float | None] = mapped_column(default=None)
    opened_at: Mapped[datetime]
    closed_at: Mapped[datetime | None] = mapped_column(default=None)
    realized_pl: Mapped[float | None] = mapped_column(default=None)
    return_pct: Mapped[float | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(String(8), default="open")


class Position(Base):
    __tablename__ = "position"
    __table_args__ = (UniqueConstraint("instrument_id", name="uq_position_instrument"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), index=True)
    qty: Mapped[int]
    avg_entry_price: Mapped[float]
    opened_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(8), default="open")
    stop_price: Mapped[float | None] = mapped_column(default=None)
    take_profit_price: Mapped[float | None] = mapped_column(default=None)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    cash: Mapped[float]
    equity: Mapped[float]
    buying_power: Mapped[float]
    unrealized_pl: Mapped[float] = mapped_column(default=0.0)
    realized_pl: Mapped[float] = mapped_column(default=0.0)
    gross_exposure: Mapped[float] = mapped_column(default=0.0)
    net_exposure: Mapped[float] = mapped_column(default=0.0)
    drawdown: Mapped[float] = mapped_column(default=0.0)
    peak_equity: Mapped[float] = mapped_column(default=0.0)
    sector_allocation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reconciled: Mapped[bool] = mapped_column(default=True)


class TradeProposalRow(Base):
    """Decision-journal entry (Story 3.7): the operator-facing record of every
    non-HOLD decision — executed autonomously, risk-vetoed, or (optional
    approval mode) pending/approved/rejected/expired."""

    __tablename__ = "trade_proposal"

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decision.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(4))
    proposed_qty: Mapped[int]
    executed_qty: Mapped[int] = mapped_column(default=0)
    rationale: Mapped[str] = mapped_column(Text, default="")
    inputs_ref: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(index=True)
    expires_at: Mapped[datetime | None] = mapped_column(default=None)
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
    decided_by: Mapped[str | None] = mapped_column(String(32), default=None)


class NewsItemRow(Base):
    """Persisted, deduplicated news/filing item (Story 3.3). The UNIQUE
    ``content_hash`` collapses the same story across sources/cycles so it is
    never stored twice — nor re-sent to Gemini — within its retention window."""

    __tablename__ = "news_item"
    __table_args__ = (UniqueConstraint("content_hash", name="uq_news_item_content_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(String(64))
    headline: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str | None] = mapped_column(String(1024), default=None)
    published_at: Mapped[datetime] = mapped_column(index=True)
    fetched_at: Mapped[datetime]


class SocialDigestRow(Base):
    """Persisted per-symbol social digest snapshot (Story 3.3). Prior rows feed
    the rolling mention-volume baseline the Stage-1 aggregator compares against."""

    __tablename__ = "social_digest"

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), index=True)
    generated_at: Mapped[datetime] = mapped_column(index=True)
    qualifying_post_count: Mapped[int]
    bull_count: Mapped[int]
    bear_count: Mapped[int]
    bull_bear_ratio: Mapped[float]
    mention_volume: Mapped[int]
    baseline_volume: Mapped[float]
    volume_ratio: Mapped[float]
    anomaly_flag: Mapped[bool]
    top_posts: Mapped[list[Any]] = mapped_column(JSON, default=list)


class SystemControl(Base):
    __tablename__ = "system_control"
    __table_args__ = (UniqueConstraint("key", name="uq_system_control_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[str]
    updated_at: Mapped[datetime]
    updated_by: Mapped[str] = mapped_column(String(32), default="system")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    actor: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(64))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    correlation_id: Mapped[str | None] = mapped_column(String(36), default=None)
