"""Repository classes — the only code in CLAV that issues SQL (docs/03-database.md,
docs/05-class-design.md §2). Each repository converts between domain Pydantic
models and the SQLAlchemy rows in ``clav.data.tables``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from clav.data import tables
from clav.domain.models import (
    AnalysisResult,
    Candle,
    ConfigSnapshot,
    EarningsEvent,
    Fill,
    HealthEvent,
    IndicatorSet,
    NewsItem,
    Order,
    OrderRequest,
    PortfolioSnapshot,
    PromptVersion,
    RiskDecision,
    SocialDigest,
    SocialItem,
    TradeProposal,
)
from clav.domain.models import (
    Position as PositionModel,
)


class InstrumentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_or_create(self, symbol: str, *, sector: str | None = None) -> tables.Instrument:
        """``sector`` (Story 2.6) only seeds a **new** instrument row — it never
        overwrites an already-tagged one, so a later config change or manual
        correction to ``instrument.sector`` isn't silently clobbered on the
        next scan cycle."""
        symbol = symbol.upper()
        row = self._session.scalar(
            select(tables.Instrument).where(tables.Instrument.symbol == symbol)
        )
        if row is None:
            row = tables.Instrument(symbol=symbol, sector=sector)
            self._session.add(row)
            self._session.flush()
        return row

    def get_by_symbol(self, symbol: str) -> tables.Instrument | None:
        return self._session.scalar(
            select(tables.Instrument).where(tables.Instrument.symbol == symbol.upper())
        )

    def get_by_id(self, instrument_id: int) -> tables.Instrument | None:
        return self._session.get(tables.Instrument, instrument_id)


class CandleRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_many(self, instrument_id: int, candles: list[Candle]) -> None:
        for candle in candles:
            existing = self._session.scalar(
                select(tables.Candle).where(
                    tables.Candle.instrument_id == instrument_id,
                    tables.Candle.timeframe == candle.timeframe,
                    tables.Candle.ts == candle.ts,
                )
            )
            if existing is not None:
                existing.open = candle.open
                existing.high = candle.high
                existing.low = candle.low
                existing.close = candle.close
                existing.volume = candle.volume
                continue
            self._session.add(
                tables.Candle(
                    instrument_id=instrument_id,
                    timeframe=candle.timeframe,
                    open=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    volume=candle.volume,
                    ts=candle.ts,
                )
            )
        self._session.flush()

    def latest_ts(self, instrument_id: int, timeframe: str) -> datetime | None:
        return self._session.scalar(
            select(tables.Candle.ts)
            .where(
                tables.Candle.instrument_id == instrument_id,
                tables.Candle.timeframe == timeframe,
            )
            .order_by(tables.Candle.ts.desc())
            .limit(1)
        )

    def get_recent(self, instrument_id: int, timeframe: str, limit: int) -> list[Candle]:
        rows = self._session.scalars(
            select(tables.Candle)
            .where(
                tables.Candle.instrument_id == instrument_id,
                tables.Candle.timeframe == timeframe,
            )
            .order_by(tables.Candle.ts.desc())
            .limit(limit)
        ).all()
        symbol = self._session.get(tables.Instrument, instrument_id)
        symbol_str = symbol.symbol if symbol is not None else ""
        return [
            Candle(
                symbol=symbol_str,
                timeframe=r.timeframe,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                volume=r.volume,
                ts=r.ts,
            )
            for r in reversed(rows)
        ]

    def get_range(
        self, instrument_id: int, timeframe: str, *, start: datetime, end: datetime, limit: int
    ) -> list[Candle]:
        """Closes between two timestamps, oldest first, bounded (Story 5.3's
        trade-review price path -- never loads a symbol's full candle
        history for one review)."""
        rows = self._session.scalars(
            select(tables.Candle)
            .where(
                tables.Candle.instrument_id == instrument_id,
                tables.Candle.timeframe == timeframe,
                tables.Candle.ts >= start,
                tables.Candle.ts <= end,
            )
            .order_by(tables.Candle.ts.asc())
            .limit(limit)
        ).all()
        symbol = self._session.get(tables.Instrument, instrument_id)
        symbol_str = symbol.symbol if symbol is not None else ""
        return [
            Candle(
                symbol=symbol_str,
                timeframe=r.timeframe,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                volume=r.volume,
                ts=r.ts,
            )
            for r in rows
        ]


class EarningsEventRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, instrument_id: int, event: EarningsEvent) -> None:
        self._session.add(
            tables.EarningsEvent(
                instrument_id=instrument_id,
                event_type=event.event_type,
                scheduled_at=event.scheduled_at,
                confirmed=event.confirmed,
                source=event.source,
            )
        )
        self._session.flush()

    def get_upcoming(self, instrument_id: int, *, after: datetime) -> list[tables.EarningsEvent]:
        return list(
            self._session.scalars(
                select(tables.EarningsEvent).where(
                    tables.EarningsEvent.instrument_id == instrument_id,
                    tables.EarningsEvent.scheduled_at >= after,
                )
            ).all()
        )

    def exists(self, instrument_id: int, *, scheduled_at: datetime, event_type: str) -> bool:
        """Used by the earnings-calendar seed (Story 2.8) to stay idempotent
        across repeated ``startup_reconcile()`` calls/process restarts."""
        return (
            self._session.scalar(
                select(tables.EarningsEvent.id).where(
                    tables.EarningsEvent.instrument_id == instrument_id,
                    tables.EarningsEvent.scheduled_at == scheduled_at,
                    tables.EarningsEvent.event_type == event_type,
                )
            )
            is not None
        )


class IndicatorSetRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, instrument_id: int, iset: IndicatorSet) -> None:
        self._session.add(
            tables.IndicatorSet(
                instrument_id=instrument_id,
                ts=iset.ts,
                close=iset.close,
                sma_20=iset.sma_20,
                ema_50=iset.ema_50,
                rsi_14=iset.rsi_14,
                macd=iset.macd,
                macd_signal=iset.macd_signal,
                atr_14=iset.atr_14,
                bb_upper=iset.bb_upper,
                bb_lower=iset.bb_lower,
                vol_avg_20=iset.vol_avg_20,
                technical_score=iset.technical_score,
            )
        )
        self._session.flush()

    def latest_ts(self, instrument_id: int) -> datetime | None:
        return self._session.scalar(
            select(tables.IndicatorSet.ts)
            .where(tables.IndicatorSet.instrument_id == instrument_id)
            .order_by(tables.IndicatorSet.ts.desc())
            .limit(1)
        )


class ScanCycleRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, cycle_id: str, *, started_at: datetime, mode: str, trigger: str) -> None:
        self._session.add(
            tables.ScanCycle(
                id=cycle_id,
                started_at=started_at,
                mode=mode,
                market_open=False,
                trigger=trigger,
                status="running",
            )
        )
        self._session.flush()

    def set_market_open(self, cycle_id: str, market_open: bool) -> None:
        row = self._session.get(tables.ScanCycle, cycle_id)
        if row is not None:
            row.market_open = market_open

    def finish(self, cycle_id: str, *, finished_at: datetime, status: str) -> None:
        row = self._session.get(tables.ScanCycle, cycle_id)
        if row is not None:
            row.finished_at = finished_at
            row.status = status

    def get(self, cycle_id: str) -> tables.ScanCycle | None:
        return self._session.get(tables.ScanCycle, cycle_id)

    def latest(self) -> tables.ScanCycle | None:
        return self._session.scalar(
            select(tables.ScanCycle).order_by(tables.ScanCycle.started_at.desc()).limit(1)
        )


class DecisionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self, *, scan_cycle_id: str, instrument_id: int, decision: Any, created_at: datetime
    ) -> int:
        row = tables.Decision(
            scan_cycle_id=scan_cycle_id,
            instrument_id=instrument_id,
            action=decision.action,
            raw_score=decision.raw_score,
            technical_score=decision.technical_score,
            llm_signal=decision.llm_signal,
            portfolio_bias=decision.portfolio_bias,
            target_qty=decision.target_qty,
            reasoning=decision.reasoning,
            created_at=created_at,
        )
        self._session.add(row)
        self._session.flush()
        return row.id

    def get(self, decision_id: int) -> tables.Decision | None:
        return self._session.get(tables.Decision, decision_id)

    def list_recent(
        self,
        *,
        symbol: str | None = None,
        action: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[tables.Decision]:
        """Newest-first, filterable, bounded (Story 4.6) -- the AI-explanation
        view's list query. An unknown ``symbol`` returns an empty page rather
        than erroring."""
        stmt = select(tables.Decision)
        if symbol is not None:
            instrument = self._session.scalar(
                select(tables.Instrument).where(tables.Instrument.symbol == symbol.upper())
            )
            if instrument is None:
                return []
            stmt = stmt.where(tables.Decision.instrument_id == instrument.id)
        if action is not None:
            stmt = stmt.where(tables.Decision.action == action.upper())
        stmt = stmt.order_by(tables.Decision.created_at.desc()).limit(limit).offset(offset)
        return list(self._session.scalars(stmt).all())

    def count_recent(self, *, symbol: str | None = None, action: str | None = None) -> int:
        stmt = select(func.count()).select_from(tables.Decision)
        if symbol is not None:
            instrument = self._session.scalar(
                select(tables.Instrument).where(tables.Instrument.symbol == symbol.upper())
            )
            if instrument is None:
                return 0
            stmt = stmt.where(tables.Decision.instrument_id == instrument.id)
        if action is not None:
            stmt = stmt.where(tables.Decision.action == action.upper())
        return self._session.scalar(stmt) or 0

    def count_by_action_for_cycle(self, scan_cycle_id: str) -> dict[str, int]:
        rows = self._session.execute(
            select(tables.Decision.action, func.count())
            .where(tables.Decision.scan_cycle_id == scan_cycle_id)
            .group_by(tables.Decision.action)
        ).all()
        return {action: count for action, count in rows}

    def list_by_cycle(self, scan_cycle_id: str) -> list[tables.Decision]:
        """Every decision made in one cycle (Story 4.7's "reconstruct this
        cycle" view) — inherently bounded by the watchlist size."""
        return list(
            self._session.scalars(
                select(tables.Decision)
                .where(tables.Decision.scan_cycle_id == scan_cycle_id)
                .order_by(tables.Decision.created_at.asc())
            ).all()
        )


class RiskEvaluationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, decision_id: int, decision: RiskDecision, *, evaluated_at: datetime) -> None:
        self._session.add(
            tables.RiskEvaluation(
                decision_id=decision_id,
                approved=decision.approved,
                adjusted_qty=decision.adjusted_qty,
                blocked_by=decision.blocked_by,
                notes=decision.notes,
                evaluated_at=evaluated_at,
            )
        )
        self._session.flush()

    def get_by_decision_id(self, decision_id: int) -> tables.RiskEvaluation | None:
        return self._session.scalar(
            select(tables.RiskEvaluation).where(tables.RiskEvaluation.decision_id == decision_id)
        )


class OrderRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, order_id: int) -> tables.Order | None:
        return self._session.get(tables.Order, order_id)

    def get_by_client_order_id(self, client_order_id: str) -> tables.Order | None:
        return self._session.scalar(
            select(tables.Order).where(tables.Order.client_order_id == client_order_id)
        )

    def create(
        self,
        *,
        instrument_id: int,
        decision_id: int | None,
        request: OrderRequest,
        submitted_at: datetime,
    ) -> tables.Order:
        row = tables.Order(
            decision_id=decision_id,
            instrument_id=instrument_id,
            client_order_id=request.client_order_id,
            side=request.side,
            order_type=request.order_type,
            qty=request.qty,
            limit_price=request.limit_price,
            status="new",
            submitted_at=submitted_at,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def update_from_broker_order(self, client_order_id: str, order: Order) -> None:
        row = self.get_by_client_order_id(client_order_id)
        if row is None:
            return
        row.broker_order_id = order.broker_order_id
        row.status = order.status
        row.updated_at = order.updated_at
        row.error = order.error

    def get_open_orders(self) -> list[tables.Order]:
        open_statuses = ("new", "accepted", "partially_filled")
        return list(
            self._session.scalars(
                select(tables.Order).where(tables.Order.status.in_(open_statuses))
            ).all()
        )

    def get_by_decision_id(self, decision_id: int) -> tables.Order | None:
        return self._session.scalar(
            select(tables.Order).where(tables.Order.decision_id == decision_id)
        )

    def count_by_status_for_cycle(self, scan_cycle_id: str) -> dict[str, int]:
        rows = self._session.execute(
            select(tables.Order.status, func.count())
            .join(tables.Decision, tables.Order.decision_id == tables.Decision.id)
            .where(tables.Decision.scan_cycle_id == scan_cycle_id)
            .group_by(tables.Order.status)
        ).all()
        return {status: count for status, count in rows}


class FillRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, order_id: int, fill: Fill) -> None:
        existing = self._session.scalar(
            select(tables.Fill).where(
                tables.Fill.order_id == order_id,
                tables.Fill.broker_fill_id == fill.broker_fill_id,
            )
        )
        if existing is not None and fill.broker_fill_id is not None:
            return
        self._session.add(
            tables.Fill(
                order_id=order_id,
                qty=fill.qty,
                price=fill.price,
                fee=fill.fee,
                filled_at=fill.filled_at,
                broker_fill_id=fill.broker_fill_id,
            )
        )
        self._session.flush()

    def get_by_order_id(self, order_id: int) -> list[tables.Fill]:
        return list(
            self._session.scalars(
                select(tables.Fill)
                .where(tables.Fill.order_id == order_id)
                .order_by(tables.Fill.filled_at.asc())
            ).all()
        )


class TradeRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, trade_id: int) -> tables.Trade | None:
        return self._session.get(tables.Trade, trade_id)

    def open_trade(
        self,
        *,
        instrument_id: int,
        entry_order_id: int,
        entry_decision_id: int | None,
        qty: int,
        entry_price: float,
        opened_at: datetime,
    ) -> tables.Trade:
        row = tables.Trade(
            instrument_id=instrument_id,
            entry_order_id=entry_order_id,
            entry_decision_id=entry_decision_id,
            qty=qty,
            entry_price=entry_price,
            opened_at=opened_at,
            status="open",
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get_open_trade(self, instrument_id: int) -> tables.Trade | None:
        return self._session.scalar(
            select(tables.Trade).where(
                tables.Trade.instrument_id == instrument_id, tables.Trade.status == "open"
            )
        )

    def close_trade(
        self,
        trade_id: int,
        *,
        exit_order_id: int,
        exit_price: float,
        closed_at: datetime,
        realized_pl: float,
        return_pct: float,
    ) -> None:
        row = self._session.get(tables.Trade, trade_id)
        if row is None:
            return
        row.exit_order_id = exit_order_id
        row.exit_price = exit_price
        row.closed_at = closed_at
        row.realized_pl = realized_pl
        row.return_pct = return_pct
        row.status = "closed"

    def get_last_closed_trade(self, instrument_id: int) -> tables.Trade | None:
        """Used by ``CooldownRule`` (Story 2.9) for the per-symbol cooldown —
        the last time a trade in this symbol *closed*, not opened; an open
        position is already excluded from new entries by ``DecisionEngine``'s
        own holding check, so this is specifically about not immediately
        re-entering right after exiting."""
        return self._session.scalar(
            select(tables.Trade)
            .where(tables.Trade.instrument_id == instrument_id, tables.Trade.status == "closed")
            .order_by(tables.Trade.closed_at.desc())
            .limit(1)
        )

    def get_last_loss(self) -> tables.Trade | None:
        """Used by ``CooldownRule`` (Story 2.9) for the global post-loss
        cooldown — the most recent realized loss across every symbol."""
        return self._session.scalar(
            select(tables.Trade)
            .where(tables.Trade.status == "closed", tables.Trade.realized_pl < 0)
            .order_by(tables.Trade.closed_at.desc())
            .limit(1)
        )

    def get_by_entry_decision_id(self, decision_id: int) -> tables.Trade | None:
        """Story 4.6: the trade (incl. realized P&L once closed) that a
        BUY/SELL decision led to, if any."""
        return self._session.scalar(
            select(tables.Trade).where(tables.Trade.entry_decision_id == decision_id)
        )

    def list_closed(self, *, limit: int = 500) -> list[tables.Trade]:
        """Closed trades, most-recently-closed-first, bounded (Story 4.9's
        calibration view -- never scans the whole trade table)."""
        return list(
            self._session.scalars(
                select(tables.Trade)
                .where(tables.Trade.status == "closed")
                .order_by(tables.Trade.closed_at.desc())
                .limit(limit)
            ).all()
        )

    def list_pending_reviews(self, *, now: datetime, limit: int = 50) -> list[tables.Trade]:
        """Epic 5's review-pass query-as-queue (epic-05 decision #1): closed
        trades still awaiting a review, oldest-closed-first so a backlog
        drains in the order trades actually closed. A trade already
        ``reviewed`` or terminally ``failed`` (Story 5.4) never reappears
        here without a manual rerun resetting it back to ``pending``, and one
        serving an exponential backoff (``review_next_attempt_at`` in the
        future) is skipped until it elapses (Story 5.4, epic-05 decision #5)."""
        return list(
            self._session.scalars(
                select(tables.Trade)
                .where(
                    tables.Trade.status == "closed",
                    tables.Trade.review_status == "pending",
                    (tables.Trade.review_next_attempt_at.is_(None))
                    | (tables.Trade.review_next_attempt_at <= now),
                )
                .order_by(tables.Trade.closed_at.asc())
                .limit(limit)
            ).all()
        )

    def mark_reviewed(self, trade_id: int) -> None:
        row = self._session.get(tables.Trade, trade_id)
        if row is not None:
            row.review_status = "reviewed"
            row.review_next_attempt_at = None

    def mark_review_attempt_failed(
        self, trade_id: int, *, attempts: int, next_attempt_at: datetime
    ) -> None:
        """A genuine ``ReviewError`` under ``max_attempts`` -- stays
        ``pending`` so ``list_pending_reviews`` retries it, but not before
        ``next_attempt_at`` (exponential backoff, Story 5.4)."""
        row = self._session.get(tables.Trade, trade_id)
        if row is not None:
            row.review_attempts = attempts
            row.review_next_attempt_at = next_attempt_at

    def mark_review_failed(self, trade_id: int, *, attempts: int) -> None:
        """``max_attempts`` reached -- terminal, excluded from every future
        pass until a manual rerun (Story 5.7) resets it (Story 5.4, epic-05
        decision #5)."""
        row = self._session.get(tables.Trade, trade_id)
        if row is not None:
            row.review_status = "failed"
            row.review_attempts = attempts
            row.review_next_attempt_at = None


class TradeReviewRepository:
    """Persist + query the Epic-5 trade-review journal (Story 5.1,
    docs/07-trade-review.md). Append-only: ``insert`` never updates an
    existing row, so a manual re-review (Story 5.7, epic-05 decision #6)
    simply adds another row for the same ``trade_id`` rather than
    overwriting the last one."""

    # Bounds how much review history a dashboard/aggregation query reads,
    # regardless of how large the journal has grown (Pi RAM discipline --
    # same instinct as web/calibration.py's MAX_TRADES).
    MAX_RECENT = 500

    def __init__(self, session: Session) -> None:
        self._session = session

    def insert(
        self,
        trade_id: int,
        *,
        created_at: datetime,
        model: str,
        why_entered: str,
        supporting_info: list[str],
        risks_at_entry: list[str],
        reasoning_correct: bool | None,
        what_worked: list[str],
        misleading_signals: list[str],
        hindsight_view: str,
        improvements: list[str],
        confidence_calibration: str,
        tags: list[str],
        raw_response: dict[str, Any],
    ) -> tables.TradeReviewRow:
        row = tables.TradeReviewRow(
            trade_id=trade_id,
            created_at=created_at,
            model=model,
            why_entered=why_entered,
            supporting_info=supporting_info,
            risks_at_entry=risks_at_entry,
            reasoning_correct=reasoning_correct,
            what_worked=what_worked,
            misleading_signals=misleading_signals,
            hindsight_view=hindsight_view,
            improvements=improvements,
            confidence_calibration=confidence_calibration,
            tags=tags,
            raw_response=raw_response,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def list_for_trade(self, trade_id: int) -> list[tables.TradeReviewRow]:
        """Full review history for one trade, newest first -- a re-reviewed
        trade shows every past verdict, not just the latest (epic-05
        decision #6)."""
        return list(
            self._session.scalars(
                select(tables.TradeReviewRow)
                .where(tables.TradeReviewRow.trade_id == trade_id)
                .order_by(tables.TradeReviewRow.created_at.desc())
            ).all()
        )

    def list_recent(
        self, *, limit: int = 100, symbol: str | None = None, calibration: str | None = None
    ) -> list[tables.TradeReviewRow]:
        """Newest-first, bounded, for the ``/reviews`` dashboard (Story 5.5).
        Tag filtering is that story's concern over this bounded page --
        there is no SQL-level JSON-array containment query here."""
        stmt = select(tables.TradeReviewRow).order_by(tables.TradeReviewRow.created_at.desc())
        if symbol is not None:
            stmt = (
                stmt.join(tables.Trade, tables.Trade.id == tables.TradeReviewRow.trade_id)
                .join(tables.Instrument, tables.Instrument.id == tables.Trade.instrument_id)
                .where(tables.Instrument.symbol == symbol.upper())
            )
        if calibration is not None:
            stmt = stmt.where(tables.TradeReviewRow.confidence_calibration == calibration)
        return list(self._session.scalars(stmt.limit(min(limit, self.MAX_RECENT))).all())

    def calibration_verdict_counts(self, *, limit: int = MAX_RECENT) -> dict[str, int]:
        """Count of reviews per ``confidence_calibration`` verdict over the
        most recent ``limit`` reviews (Story 5.6's calibration panel) --
        bounded so this never scans the full journal."""
        rows = self._session.execute(
            select(tables.TradeReviewRow.confidence_calibration)
            .order_by(tables.TradeReviewRow.created_at.desc())
            .limit(limit)
        ).all()
        counts: dict[str, int] = {}
        for (verdict,) in rows:
            counts[verdict] = counts.get(verdict, 0) + 1
        return counts

    def tag_frequency(self, *, limit: int = MAX_RECENT) -> dict[str, int]:
        """Frequency of each tag across the most recent ``limit`` reviews
        (Story 5.6) -- unpacked in Python since JSON-array containment isn't
        a first-class SQLite query here, matching web/calibration.py's
        bounded-fetch-then-aggregate style."""
        rows = self._session.execute(
            select(tables.TradeReviewRow.tags)
            .order_by(tables.TradeReviewRow.created_at.desc())
            .limit(limit)
        ).all()
        counts: dict[str, int] = {}
        for (tags,) in rows:
            for tag in tags or []:
                counts[tag] = counts.get(tag, 0) + 1
        return counts

    def misleading_signal_frequency(self, *, limit: int = MAX_RECENT) -> dict[str, int]:
        """Frequency of each ``misleading_signals`` entry across the most
        recent ``limit`` reviews (Story 5.6) -- same bounded-fetch-then-
        aggregate shape as ``tag_frequency``."""
        rows = self._session.execute(
            select(tables.TradeReviewRow.misleading_signals)
            .order_by(tables.TradeReviewRow.created_at.desc())
            .limit(limit)
        ).all()
        counts: dict[str, int] = {}
        for (signals,) in rows:
            for signal in signals or []:
                counts[signal] = counts.get(signal, 0) + 1
        return counts


class PositionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, instrument_id: int) -> tables.Position | None:
        return self._session.scalar(
            select(tables.Position).where(tables.Position.instrument_id == instrument_id)
        )

    def get_all(self) -> list[tables.Position]:
        return list(self._session.scalars(select(tables.Position)).all())

    def upsert(self, instrument_id: int, position: PositionModel, *, opened_at: datetime) -> None:
        row = self.get(instrument_id)
        if row is None:
            row = tables.Position(
                instrument_id=instrument_id,
                qty=position.qty,
                avg_entry_price=position.avg_entry_price,
                opened_at=opened_at,
                status="open",
                stop_price=position.stop_price,
                take_profit_price=position.take_profit_price,
            )
            self._session.add(row)
        else:
            row.qty = position.qty
            row.avg_entry_price = position.avg_entry_price
            row.stop_price = position.stop_price
            row.take_profit_price = position.take_profit_price
            row.status = "open" if position.qty != 0 else "closed"
        self._session.flush()

    def delete(self, instrument_id: int) -> None:
        row = self.get(instrument_id)
        if row is not None:
            self._session.delete(row)
            self._session.flush()


class PortfolioSnapshotRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, snapshot: PortfolioSnapshot) -> None:
        self._session.add(
            tables.PortfolioSnapshot(
                ts=snapshot.ts,
                cash=snapshot.cash,
                equity=snapshot.equity,
                buying_power=snapshot.buying_power,
                unrealized_pl=snapshot.unrealized_pl,
                realized_pl=snapshot.realized_pl,
                gross_exposure=snapshot.gross_exposure,
                net_exposure=snapshot.net_exposure,
                drawdown=snapshot.drawdown,
                peak_equity=snapshot.peak_equity,
                sector_allocation=snapshot.sector_allocation,
                reconciled=snapshot.reconciled,
            )
        )
        self._session.flush()

    def latest(self) -> tables.PortfolioSnapshot | None:
        return self._session.scalar(
            select(tables.PortfolioSnapshot).order_by(tables.PortfolioSnapshot.ts.desc()).limit(1)
        )

    def get_recent(self, *, limit: int) -> list[tables.PortfolioSnapshot]:
        """The last ``limit`` snapshots, oldest-first (chart plotting order).
        Bounded at the query level (Story 4.5) — a chart page never loads the
        full history into RAM."""
        rows = self._session.scalars(
            select(tables.PortfolioSnapshot)
            .order_by(tables.PortfolioSnapshot.ts.desc())
            .limit(limit)
        ).all()
        return list(reversed(rows))

    def get_since(self, cutoff: datetime, *, limit: int) -> list[tables.PortfolioSnapshot]:
        """Every snapshot at/after ``cutoff``, oldest-first, bounded at the
        query level -- the dashboard's period-selectable (1H/1D/1W/YTD/1Y)
        equity chart never loads more than ``limit`` rows regardless of how
        far back ``cutoff`` reaches."""
        rows = self._session.scalars(
            select(tables.PortfolioSnapshot)
            .where(tables.PortfolioSnapshot.ts >= cutoff)
            .order_by(tables.PortfolioSnapshot.ts.desc())
            .limit(limit)
        ).all()
        return list(reversed(rows))

    def get_last_before(self, cutoff: datetime) -> tables.PortfolioSnapshot | None:
        """The most recent snapshot at/before ``cutoff`` -- the baseline point
        a period's change is measured from (e.g. "1W" measures equity change
        since the last snapshot at-or-before ``now - 7 days``)."""
        return self._session.scalar(
            select(tables.PortfolioSnapshot)
            .where(tables.PortfolioSnapshot.ts <= cutoff)
            .order_by(tables.PortfolioSnapshot.ts.desc())
            .limit(1)
        )


class PromptVersionRepository:
    """Versioned persona/strategy-prompt store (Story 3.10)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _to_domain(self, row: tables.PromptVersionRow) -> PromptVersion:
        return PromptVersion(
            id=row.id,
            content=row.content,
            created_at=row.created_at,
            created_by=row.created_by,
            active=row.active,
        )

    def get(self, version_id: int) -> PromptVersion | None:
        row = self._session.get(tables.PromptVersionRow, version_id)
        return self._to_domain(row) if row is not None else None

    def get_active(self) -> PromptVersion | None:
        row = self._session.scalar(
            select(tables.PromptVersionRow).where(tables.PromptVersionRow.active.is_(True))
        )
        return self._to_domain(row) if row is not None else None

    def list_versions(self, *, limit: int = 20) -> list[PromptVersion]:
        rows = self._session.scalars(
            select(tables.PromptVersionRow)
            .order_by(tables.PromptVersionRow.created_at.desc())
            .limit(limit)
        ).all()
        return [self._to_domain(r) for r in rows]

    def activate(self, version_id: int) -> PromptVersion | None:
        """Atomically make ``version_id`` the sole active row."""
        target = self._session.get(tables.PromptVersionRow, version_id)
        if target is None:
            return None
        self._session.execute(
            update(tables.PromptVersionRow)
            .where(tables.PromptVersionRow.active.is_(True))
            .values(active=False)
        )
        target.active = True
        self._session.flush()
        return self._to_domain(target)

    def create_and_activate(
        self, *, content: str, created_by: str, created_at: datetime
    ) -> PromptVersion:
        """The "edit the prompt" flow: a new immutable version, made active
        atomically. The previously-active row is retained (history), not
        deleted."""
        self._session.execute(
            update(tables.PromptVersionRow)
            .where(tables.PromptVersionRow.active.is_(True))
            .values(active=False)
        )
        row = tables.PromptVersionRow(
            content=content, created_at=created_at, created_by=created_by, active=True
        )
        self._session.add(row)
        self._session.flush()
        return self._to_domain(row)

    def seed_default_if_missing(
        self, *, content: str, created_by: str, created_at: datetime
    ) -> PromptVersion:
        """Idempotent startup seed: only creates the default persona if no
        active version exists yet (never clobbers an operator's edit)."""
        active = self.get_active()
        if active is not None:
            return active
        return self.create_and_activate(
            content=content, created_by=created_by, created_at=created_at
        )


class AnalysisResultRepository:
    """Persist + retention for the redacted Gemini request/response provenance
    (Story 3.12 closure)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, instrument_id: int, result: AnalysisResult) -> int:
        row = tables.AnalysisResultRow(
            instrument_id=instrument_id,
            created_at=result.created_at,
            model=result.model,
            prompt_version=result.prompt_version,
            sentiment=result.sentiment,
            conviction=result.conviction,
            is_fallback=result.is_fallback,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            request=result.request,
            response=result.response,
        )
        self._session.add(row)
        self._session.flush()
        return row.id

    def get(self, analysis_id: int) -> AnalysisResult | None:
        row = self._session.get(tables.AnalysisResultRow, analysis_id)
        if row is None:
            return None
        instrument = self._session.get(tables.Instrument, row.instrument_id)
        return AnalysisResult(
            id=row.id,
            symbol=instrument.symbol if instrument is not None else "",
            model=row.model,
            prompt_version=row.prompt_version,
            sentiment=row.sentiment,
            conviction=row.conviction,
            is_fallback=row.is_fallback,
            prompt_tokens=row.prompt_tokens,
            completion_tokens=row.completion_tokens,
            request=row.request,
            response=row.response,
            created_at=row.created_at,
        )

    def prune(self, instrument_id: int, *, keep: int) -> int:
        keep_ids = self._session.scalars(
            select(tables.AnalysisResultRow.id)
            .where(tables.AnalysisResultRow.instrument_id == instrument_id)
            .order_by(tables.AnalysisResultRow.created_at.desc())
            .limit(keep)
        ).all()
        stale_rows = self._session.scalars(
            select(tables.AnalysisResultRow).where(
                tables.AnalysisResultRow.instrument_id == instrument_id,
                tables.AnalysisResultRow.id.notin_(keep_ids),
            )
        ).all()
        for row in stale_rows:
            self._session.delete(row)
        self._session.flush()
        return len(stale_rows)


class TradeProposalRepository:
    """The decision journal (Story 3.7): every non-HOLD decision — executed,
    vetoed, or (optional approval mode) pending/approved/rejected/expired."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _to_domain(self, row: tables.TradeProposalRow) -> TradeProposal:
        return TradeProposal(
            id=row.id,
            decision_id=row.decision_id,
            symbol=row.symbol,
            side=row.side,
            proposed_qty=row.proposed_qty,
            executed_qty=row.executed_qty,
            rationale=row.rationale,
            inputs_ref=row.inputs_ref,
            status=row.status,
            created_at=row.created_at,
            expires_at=row.expires_at,
            decided_at=row.decided_at,
            decided_by=row.decided_by,
        )

    def create(
        self,
        *,
        decision_id: int,
        symbol: str,
        side: str,
        proposed_qty: int,
        rationale: str,
        inputs_ref: dict[str, Any],
        status: str,
        created_at: datetime,
        executed_qty: int = 0,
        expires_at: datetime | None = None,
        decided_at: datetime | None = None,
        decided_by: str | None = None,
    ) -> TradeProposal:
        row = tables.TradeProposalRow(
            decision_id=decision_id,
            symbol=symbol.upper(),
            side=side,
            proposed_qty=proposed_qty,
            executed_qty=executed_qty,
            rationale=rationale,
            inputs_ref=inputs_ref,
            status=status,
            created_at=created_at,
            expires_at=expires_at,
            decided_at=decided_at,
            decided_by=decided_by,
        )
        self._session.add(row)
        self._session.flush()
        return self._to_domain(row)

    def get(self, proposal_id: int) -> TradeProposal | None:
        row = self._session.get(tables.TradeProposalRow, proposal_id)
        return self._to_domain(row) if row is not None else None

    def get_by_decision_id(self, decision_id: int) -> TradeProposal | None:
        row = self._session.scalar(
            select(tables.TradeProposalRow).where(
                tables.TradeProposalRow.decision_id == decision_id
            )
        )
        return self._to_domain(row) if row is not None else None

    def get_row(self, proposal_id: int) -> tables.TradeProposalRow | None:
        return self._session.get(tables.TradeProposalRow, proposal_id)

    def mark_decided(
        self,
        proposal_id: int,
        *,
        status: str,
        decided_at: datetime,
        decided_by: str,
        executed_qty: int | None = None,
    ) -> TradeProposal | None:
        row = self._session.get(tables.TradeProposalRow, proposal_id)
        if row is None:
            return None
        row.status = status
        row.decided_at = decided_at
        row.decided_by = decided_by
        if executed_qty is not None:
            row.executed_qty = executed_qty
        self._session.flush()
        return self._to_domain(row)

    def expire_stale(self, now: datetime) -> int:
        """Fail-closed sweep: any ``pending`` proposal past its ``expires_at``
        becomes ``expired`` and never executes. Returns the number expired."""
        rows = self._session.scalars(
            select(tables.TradeProposalRow).where(
                tables.TradeProposalRow.status == "pending",
                tables.TradeProposalRow.expires_at.is_not(None),
                tables.TradeProposalRow.expires_at <= now,
            )
        ).all()
        for row in rows:
            row.status = "expired"
            row.decided_at = now
            row.decided_by = "system:ttl_expiry"
        self._session.flush()
        return len(rows)

    def list_recent(self, *, limit: int = 50) -> list[TradeProposal]:
        rows = self._session.scalars(
            select(tables.TradeProposalRow)
            .order_by(tables.TradeProposalRow.created_at.desc())
            .limit(limit)
        ).all()
        return [self._to_domain(r) for r in rows]

    def list_pending(self, *, symbol: str | None = None) -> list[TradeProposal]:
        stmt = select(tables.TradeProposalRow).where(tables.TradeProposalRow.status == "pending")
        if symbol is not None:
            stmt = stmt.where(tables.TradeProposalRow.symbol == symbol.upper())
        rows = self._session.scalars(stmt.order_by(tables.TradeProposalRow.created_at.desc())).all()
        return [self._to_domain(r) for r in rows]

    def list_by_status(self, status: str) -> list[TradeProposal]:
        rows = self._session.scalars(
            select(tables.TradeProposalRow)
            .where(tables.TradeProposalRow.status == status)
            .order_by(tables.TradeProposalRow.created_at.asc())
        ).all()
        return [self._to_domain(r) for r in rows]

    def mark_approved(
        self, proposal_id: int, *, decided_by: str, decided_at: datetime
    ) -> TradeProposal | None:
        """DB-only ``pending`` → ``approved`` transition — no broker access,
        safe to call from the separate ``clav-web`` process (Story 3.8,
        "never exposes brokerage keys"). ``DecisionJournal.execute_pending_
        approvals()`` (running inside ``clav-core``, which owns the broker)
        performs the actual submission on its next cycle. Fail-closed no-op
        once the proposal is no longer ``pending``."""
        row = self._session.get(tables.TradeProposalRow, proposal_id)
        if row is None:
            return None
        if row.status != "pending":
            return self._to_domain(row)
        row.status = "approved"
        row.decided_at = decided_at
        row.decided_by = decided_by
        self._session.flush()
        return self._to_domain(row)

    def mark_rejected(
        self, proposal_id: int, *, decided_by: str, decided_at: datetime
    ) -> TradeProposal | None:
        """DB-only ``pending`` → ``rejected`` transition — safe from the web
        process (never touches the broker). Fail-closed no-op once the
        proposal is no longer ``pending``."""
        row = self._session.get(tables.TradeProposalRow, proposal_id)
        if row is None:
            return None
        if row.status != "pending":
            return self._to_domain(row)
        row.status = "rejected"
        row.decided_at = decided_at
        row.decided_by = decided_by
        self._session.flush()
        return self._to_domain(row)


class NewsItemRepository:
    """Persist + dedup + retention for news/filing items (Story 3.3)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def exists(self, content_hash: str) -> bool:
        return (
            self._session.scalar(
                select(tables.NewsItemRow.id).where(tables.NewsItemRow.content_hash == content_hash)
            )
            is not None
        )

    def get_by_id(self, item_id: int) -> NewsItem | None:
        row = self._session.get(tables.NewsItemRow, item_id)
        if row is None:
            return None
        instrument = self._session.get(tables.Instrument, row.instrument_id)
        return self._to_domain(
            row, instrument.symbol if instrument is not None else "", is_stale=False
        )

    def add_many(self, instrument_id: int, items: list[NewsItem]) -> list[NewsItem]:
        """Insert only content-hashes not already stored (dedup across sources +
        cycles). Returns the items that were actually persisted (i.e. new)."""
        inserted: list[NewsItem] = []
        seen_this_batch: set[str] = set()
        for item in items:
            content_hash = item.content_hash
            if content_hash in seen_this_batch or self.exists(content_hash):
                continue
            seen_this_batch.add(content_hash)
            self._session.add(
                tables.NewsItemRow(
                    instrument_id=instrument_id,
                    content_hash=content_hash,
                    external_id=item.id,
                    source=item.source,
                    headline=item.headline,
                    body=item.body,
                    url=item.url,
                    published_at=item.published_at,
                    fetched_at=item.fetched_at,
                )
            )
            inserted.append(item)
        self._session.flush()
        return inserted

    def _to_domain(self, row: tables.NewsItemRow, symbol: str, *, is_stale: bool) -> NewsItem:
        return NewsItem(
            id=row.external_id,
            symbol=symbol,
            headline=row.headline,
            body=row.body,
            url=row.url,
            source=row.source,
            published_at=row.published_at,
            fetched_at=row.fetched_at,
            is_stale=is_stale,
        )

    def get_for_analysis(
        self, instrument_id: int, *, now: datetime, max_age_hours: int, limit: int
    ) -> list[NewsItem]:
        """Fresh items only — anything older than ``max_age_hours`` is excluded
        from analysis (Story 3.3 staleness cutoff). Newest first."""
        cutoff = now - timedelta(hours=max_age_hours)
        rows = self._session.scalars(
            select(tables.NewsItemRow)
            .where(
                tables.NewsItemRow.instrument_id == instrument_id,
                tables.NewsItemRow.published_at >= cutoff,
            )
            .order_by(tables.NewsItemRow.published_at.desc())
            .limit(limit)
        ).all()
        instrument = self._session.get(tables.Instrument, instrument_id)
        symbol = instrument.symbol if instrument is not None else ""
        return [self._to_domain(r, symbol, is_stale=False) for r in rows]

    def latest_ts(self, instrument_id: int) -> datetime | None:
        return self._session.scalar(
            select(tables.NewsItemRow.published_at)
            .where(tables.NewsItemRow.instrument_id == instrument_id)
            .order_by(tables.NewsItemRow.published_at.desc())
            .limit(1)
        )

    def recent_ids_for_analysis(
        self, instrument_id: int, *, now: datetime, max_age_hours: int, limit: int
    ) -> list[int]:
        """Row ids of the fresh items used for analysis — the provenance link
        stored in the decision journal (Story 3.7)."""
        cutoff = now - timedelta(hours=max_age_hours)
        return list(
            self._session.scalars(
                select(tables.NewsItemRow.id)
                .where(
                    tables.NewsItemRow.instrument_id == instrument_id,
                    tables.NewsItemRow.published_at >= cutoff,
                )
                .order_by(tables.NewsItemRow.published_at.desc())
                .limit(limit)
            ).all()
        )

    def prune(self, instrument_id: int, *, keep: int) -> int:
        """Retain only the ``keep`` most-recent rows per symbol (Pi disk/RAM
        discipline). Returns the number of rows deleted."""
        keep_ids = self._session.scalars(
            select(tables.NewsItemRow.id)
            .where(tables.NewsItemRow.instrument_id == instrument_id)
            .order_by(tables.NewsItemRow.published_at.desc())
            .limit(keep)
        ).all()
        stale_rows = self._session.scalars(
            select(tables.NewsItemRow).where(
                tables.NewsItemRow.instrument_id == instrument_id,
                tables.NewsItemRow.id.notin_(keep_ids),
            )
        ).all()
        for row in stale_rows:
            self._session.delete(row)
        self._session.flush()
        return len(stale_rows)


class SocialDigestRepository:
    """Persist + baseline + retention for per-symbol social digests (Story 3.3)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, instrument_id: int, digest: SocialDigest) -> int:
        row = tables.SocialDigestRow(
            instrument_id=instrument_id,
            generated_at=digest.generated_at,
            qualifying_post_count=digest.qualifying_post_count,
            bull_count=digest.bull_count,
            bear_count=digest.bear_count,
            bull_bear_ratio=digest.bull_bear_ratio,
            mention_volume=digest.mention_volume,
            baseline_volume=digest.baseline_volume,
            volume_ratio=digest.volume_ratio,
            anomaly_flag=digest.anomaly_flag,
            top_posts=[p.model_dump(mode="json") for p in digest.top_posts],
        )
        self._session.add(row)
        self._session.flush()
        return row.id

    def _to_domain(self, row: tables.SocialDigestRow, symbol: str) -> SocialDigest:
        return SocialDigest(
            symbol=symbol,
            qualifying_post_count=row.qualifying_post_count,
            bull_count=row.bull_count,
            bear_count=row.bear_count,
            bull_bear_ratio=row.bull_bear_ratio,
            mention_volume=row.mention_volume,
            baseline_volume=row.baseline_volume,
            volume_ratio=row.volume_ratio,
            anomaly_flag=row.anomaly_flag,
            top_posts=[SocialItem.model_validate(p) for p in row.top_posts],
            generated_at=row.generated_at,
        )

    def get_by_id(self, digest_id: int) -> SocialDigest | None:
        row = self._session.get(tables.SocialDigestRow, digest_id)
        if row is None:
            return None
        instrument = self._session.get(tables.Instrument, row.instrument_id)
        return self._to_domain(row, instrument.symbol if instrument is not None else "")

    def latest(self, instrument_id: int) -> SocialDigest | None:
        row = self._session.scalar(
            select(tables.SocialDigestRow)
            .where(tables.SocialDigestRow.instrument_id == instrument_id)
            .order_by(tables.SocialDigestRow.generated_at.desc())
            .limit(1)
        )
        if row is None:
            return None
        instrument = self._session.get(tables.Instrument, instrument_id)
        symbol = instrument.symbol if instrument is not None else ""
        return self._to_domain(row, symbol)

    def rolling_baseline(self, instrument_id: int, *, window: int) -> float:
        """Mean mention-volume over the last ``window`` digests — the baseline the
        anomaly guard compares a new spike against. Zero when there's no history."""
        volumes = self._session.scalars(
            select(tables.SocialDigestRow.mention_volume)
            .where(tables.SocialDigestRow.instrument_id == instrument_id)
            .order_by(tables.SocialDigestRow.generated_at.desc())
            .limit(window)
        ).all()
        return sum(volumes) / len(volumes) if volumes else 0.0

    def prune(self, instrument_id: int, *, keep: int) -> int:
        keep_ids = self._session.scalars(
            select(tables.SocialDigestRow.id)
            .where(tables.SocialDigestRow.instrument_id == instrument_id)
            .order_by(tables.SocialDigestRow.generated_at.desc())
            .limit(keep)
        ).all()
        stale_rows = self._session.scalars(
            select(tables.SocialDigestRow).where(
                tables.SocialDigestRow.instrument_id == instrument_id,
                tables.SocialDigestRow.id.notin_(keep_ids),
            )
        ).all()
        for row in stale_rows:
            self._session.delete(row)
        self._session.flush()
        return len(stale_rows)


class SystemControlRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._session.scalar(
            select(tables.SystemControl).where(tables.SystemControl.key == key)
        )
        return row.value if row is not None else default

    def set(self, key: str, value: str, *, updated_at: datetime, updated_by: str) -> None:
        row = self._session.scalar(
            select(tables.SystemControl).where(tables.SystemControl.key == key)
        )
        if row is None:
            row = tables.SystemControl(
                key=key, value=value, updated_at=updated_at, updated_by=updated_by
            )
            self._session.add(row)
        else:
            row.value = value
            row.updated_at = updated_at
            row.updated_by = updated_by
        self._session.flush()


class AuditLogRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        *,
        ts: datetime,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self._session.add(
            tables.AuditLog(
                ts=ts,
                actor=actor,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                before=before,
                after=after,
                correlation_id=correlation_id,
            )
        )
        self._session.flush()

    def list_recent(
        self, *, correlation_id: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[tables.AuditLog]:
        """Newest-first, bounded (Story 4.7's audit/journal browser)."""
        stmt = select(tables.AuditLog)
        if correlation_id is not None:
            stmt = stmt.where(tables.AuditLog.correlation_id == correlation_id)
        stmt = stmt.order_by(tables.AuditLog.ts.desc()).limit(limit).offset(offset)
        return list(self._session.scalars(stmt).all())

    def count_recent(self, *, correlation_id: str | None = None) -> int:
        stmt = select(func.count()).select_from(tables.AuditLog)
        if correlation_id is not None:
            stmt = stmt.where(tables.AuditLog.correlation_id == correlation_id)
        return self._session.scalar(stmt) or 0


class HealthEventRepository:
    """Persist + retention for ``HealthMonitor`` observations (Story 4.1)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_many(self, events: list[HealthEvent]) -> None:
        for event in events:
            self._session.add(
                tables.HealthEventRow(
                    ts=event.ts,
                    category=event.category,
                    name=event.name,
                    status=event.status,
                    value=event.value,
                    cycle_id=event.cycle_id,
                )
            )
        self._session.flush()

    def _to_domain(self, row: tables.HealthEventRow) -> HealthEvent:
        return HealthEvent(
            id=row.id,
            ts=row.ts,
            category=row.category,
            name=row.name,
            status=row.status,
            value=row.value,
            cycle_id=row.cycle_id,
        )

    def list_recent(
        self,
        *,
        category: str | None = None,
        status: str | None = None,
        cycle_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[HealthEvent]:
        stmt = select(tables.HealthEventRow)
        if category is not None:
            stmt = stmt.where(tables.HealthEventRow.category == category)
        if status is not None:
            stmt = stmt.where(tables.HealthEventRow.status == status)
        if cycle_id is not None:
            stmt = stmt.where(tables.HealthEventRow.cycle_id == cycle_id)
        stmt = stmt.order_by(tables.HealthEventRow.ts.desc()).limit(limit).offset(offset)
        rows = self._session.scalars(stmt).all()
        return [self._to_domain(r) for r in rows]

    def count_recent(
        self,
        *,
        category: str | None = None,
        status: str | None = None,
        cycle_id: str | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(tables.HealthEventRow)
        if category is not None:
            stmt = stmt.where(tables.HealthEventRow.category == category)
        if status is not None:
            stmt = stmt.where(tables.HealthEventRow.status == status)
        if cycle_id is not None:
            stmt = stmt.where(tables.HealthEventRow.cycle_id == cycle_id)
        return self._session.scalar(stmt) or 0

    def latest_by_name(self, category: str, name: str) -> HealthEvent | None:
        row = self._session.scalar(
            select(tables.HealthEventRow)
            .where(
                tables.HealthEventRow.category == category,
                tables.HealthEventRow.name == name,
            )
            .order_by(tables.HealthEventRow.ts.desc())
            .limit(1)
        )
        return self._to_domain(row) if row is not None else None

    def prune(self, *, keep_per_category: int) -> int:
        """Retain only the ``keep_per_category`` most-recent rows per category
        (Pi disk discipline). Returns the number of rows deleted."""
        deleted = 0
        categories = self._session.scalars(select(tables.HealthEventRow.category).distinct()).all()
        for category in categories:
            keep_ids = self._session.scalars(
                select(tables.HealthEventRow.id)
                .where(tables.HealthEventRow.category == category)
                .order_by(tables.HealthEventRow.ts.desc())
                .limit(keep_per_category)
            ).all()
            stale_rows = self._session.scalars(
                select(tables.HealthEventRow).where(
                    tables.HealthEventRow.category == category,
                    tables.HealthEventRow.id.notin_(keep_ids),
                )
            ).all()
            for row in stale_rows:
                self._session.delete(row)
            deleted += len(stale_rows)
        self._session.flush()
        return deleted


class ConfigSnapshotRepository:
    """The effective config that produced each cycle (Story 4.4). Consecutive
    identical cycles collapse to a small pointer row (``config=None``,
    ``same_as_snapshot_id`` set to the earliest row carrying that content) so
    an unchanged config across thousands of cycles doesn't bloat the DB;
    ``_to_domain`` resolves that transparently — callers always see the full
    effective config."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_for_cycle(
        self, cycle_id: str, *, git_sha: str, config: dict[str, Any], created_at: datetime
    ) -> ConfigSnapshot:
        payload = json.dumps(config, sort_keys=True, default=str)
        content_hash = hashlib.sha256(f"{git_sha}|{payload}".encode()).hexdigest()

        latest = self._session.scalar(
            select(tables.ConfigSnapshotRow).order_by(tables.ConfigSnapshotRow.id.desc()).limit(1)
        )
        if latest is not None and latest.content_hash == content_hash:
            row = tables.ConfigSnapshotRow(
                cycle_id=cycle_id,
                git_sha=git_sha,
                content_hash=content_hash,
                config=None,
                same_as_snapshot_id=latest.same_as_snapshot_id or latest.id,
                created_at=created_at,
            )
        else:
            row = tables.ConfigSnapshotRow(
                cycle_id=cycle_id,
                git_sha=git_sha,
                content_hash=content_hash,
                config=config,
                same_as_snapshot_id=None,
                created_at=created_at,
            )
        self._session.add(row)
        self._session.flush()
        return self._to_domain(row)

    def _to_domain(self, row: tables.ConfigSnapshotRow) -> ConfigSnapshot:
        resolved_config = row.config
        if resolved_config is None and row.same_as_snapshot_id is not None:
            target = self._session.get(tables.ConfigSnapshotRow, row.same_as_snapshot_id)
            resolved_config = target.config if target is not None else None
        return ConfigSnapshot(
            id=row.id,
            cycle_id=row.cycle_id,
            git_sha=row.git_sha,
            config=resolved_config or {},
            created_at=row.created_at,
        )

    def get_by_cycle_id(self, cycle_id: str) -> ConfigSnapshot | None:
        row = self._session.scalar(
            select(tables.ConfigSnapshotRow).where(tables.ConfigSnapshotRow.cycle_id == cycle_id)
        )
        return self._to_domain(row) if row is not None else None


class Repositories:
    """Bundle of all repositories bound to one Session — the composition root
    hands one of these to services instead of a raw Session."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.instruments = InstrumentRepository(session)
        self.candles = CandleRepository(session)
        self.earnings_events = EarningsEventRepository(session)
        self.indicator_sets = IndicatorSetRepository(session)
        self.scan_cycles = ScanCycleRepository(session)
        self.decisions = DecisionRepository(session)
        self.risk_evaluations = RiskEvaluationRepository(session)
        self.orders = OrderRepository(session)
        self.fills = FillRepository(session)
        self.trades = TradeRepository(session)
        self.trade_reviews = TradeReviewRepository(session)
        self.positions = PositionRepository(session)
        self.portfolio_snapshots = PortfolioSnapshotRepository(session)
        self.trade_proposals = TradeProposalRepository(session)
        self.prompt_versions = PromptVersionRepository(session)
        self.analysis_results = AnalysisResultRepository(session)
        self.news_items = NewsItemRepository(session)
        self.social_digests = SocialDigestRepository(session)
        self.system_control = SystemControlRepository(session)
        self.audit_log = AuditLogRepository(session)
        self.health_events = HealthEventRepository(session)
        self.config_snapshots = ConfigSnapshotRepository(session)
