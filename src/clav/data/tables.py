"""SQLAlchemy ORM models for the Epic-1/Epic-2 table subset (docs/03-database.md §3).

Tables through Epic 3 are defined here (news_item, social_digest, trade_proposal,
prompt_version, analysis_result), health_event and config_snapshot (Epic 4, Stories
4.1/4.4), and trade_review (Epic 5, Story 5.1).
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
    # Epic 5 review-pass bookkeeping (Story 5.1). `review_status` is the
    # "queue": a pass selects status='closed' AND review_status='pending' —
    # a single indexed lookup, no separate queue table (epic-05 decision #1).
    review_status: Mapped[str] = mapped_column(String(8), default="pending", index=True)
    review_attempts: Mapped[int] = mapped_column(default=0)
    # Exponential backoff after a failed review attempt (Story 5.4, epic-05
    # decision #5): NULL means "eligible now" -- list_pending_reviews() only
    # excludes a trade while this is set and still in the future.
    review_next_attempt_at: Mapped[datetime | None] = mapped_column(default=None)


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


class PromptVersionRow(Base):
    """Versioned Gemini persona/strategy prompt (Story 3.10). Immutable history:
    editing inserts a new row and atomically flips ``active`` to it."""

    __tablename__ = "prompt_version"

    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    active: Mapped[bool] = mapped_column(default=False, index=True)


class AnalysisResultRow(Base):
    """Redacted Gemini request/response for one analysis call (Story 3.12
    provenance closure). Joined to the decision it drove via the
    ``analysis_result_id`` stamped into ``decision.reasoning.llm`` /
    ``trade_proposal.inputs_ref``."""

    __tablename__ = "analysis_result"

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(index=True)
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str | None] = mapped_column(String(32), default=None)
    sentiment: Mapped[float]
    conviction: Mapped[float]
    is_fallback: Mapped[bool] = mapped_column(default=False)
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    request: Mapped[str] = mapped_column(Text, default="")
    response: Mapped[str] = mapped_column(Text, default="")


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


class HealthEventRow(Base):
    """Durable per-cycle health observation (Story 4.1): freshness, external-
    service, system-resource, trading, or liveness state. ``HealthMonitor``
    writes these at the end of every cycle; ``/health``, ``/metrics``, and the
    dashboard (Epic 4) read them instead of re-deriving state."""

    __tablename__ = "health_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    category: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(8), index=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cycle_id: Mapped[str | None] = mapped_column(
        ForeignKey("scan_cycle.id"), default=None, index=True
    )


class ConfigSnapshotRow(Base):
    """The effective config that produced one cycle (Story 4.4). One row per
    ``cycle_id``; ``content_hash`` lets consecutive-identical cycles collapse
    to a small pointer (``config=None``, ``same_as_snapshot_id`` set) instead
    of duplicating the full JSON blob across thousands of unchanged cycles."""

    __tablename__ = "config_snapshot"
    __table_args__ = (UniqueConstraint("cycle_id", name="uq_config_snapshot_cycle_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[str] = mapped_column(ForeignKey("scan_cycle.id"), index=True)
    git_sha: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    same_as_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("config_snapshot.id"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(index=True)


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


class TradeReviewRow(Base):
    """A structured Gemini post-mortem for one closed trade (Story 5.1,
    docs/07-trade-review.md). Append-only: a manual re-review
    (epic-05 decision #6, Story 5.7) inserts an additional row for the same
    ``trade_id`` rather than updating the existing one, so ``created_at``
    order is the full review history for that trade."""

    __tablename__ = "trade_review"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trade.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(index=True)
    model: Mapped[str] = mapped_column(String(64), default="")
    why_entered: Mapped[str] = mapped_column(Text, default="")
    supporting_info: Mapped[list[Any]] = mapped_column(JSON, default=list)
    risks_at_entry: Mapped[list[Any]] = mapped_column(JSON, default=list)
    reasoning_correct: Mapped[bool | None] = mapped_column(default=None)
    what_worked: Mapped[list[Any]] = mapped_column(JSON, default=list)
    misleading_signals: Mapped[list[Any]] = mapped_column(JSON, default=list)
    hindsight_view: Mapped[str] = mapped_column(Text, default="")
    improvements: Mapped[list[Any]] = mapped_column(JSON, default=list)
    confidence_calibration: Mapped[str] = mapped_column(String(16))
    tags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Asset(Base):
    """Cached snapshot of an Alpaca tradeable asset (autonomous-discovery epic).

    Distinct from ``instrument``, which is created lazily only for symbols the
    bot has actually touched: ``asset`` is the *full* tradeable catalog, refreshed
    on a slow cadence, used to validate on-demand/discovered symbols and to power
    ticker search in the UI."""

    __tablename__ = "asset"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(128), default=None)
    exchange: Mapped[str | None] = mapped_column(String(16), default=None)
    tradable: Mapped[bool] = mapped_column(default=True)
    fractionable: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(index=True)


class AnalysisRequest(Base):
    """An operator "analyze this ticker now" request (autonomous-discovery epic).

    ``clav-web`` inserts a ``pending`` row; ``clav-core`` drains a bounded number
    each cycle, runs the full pipeline, links the resulting ``decision_id`` and
    marks ``done``/``failed`` — the two-process handoff, done in the DB."""

    __tablename__ = "analysis_request"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    requested_by: Mapped[str] = mapped_column(String(64), default="operator")
    requested_at: Mapped[datetime] = mapped_column(index=True)
    # pending | done | failed
    status: Mapped[str] = mapped_column(String(8), default="pending", index=True)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decision.id"), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
