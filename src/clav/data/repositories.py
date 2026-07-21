"""Repository classes — the only code in CLAV that issues SQL (docs/03-database.md,
docs/05-class-design.md §2). Each repository converts between domain Pydantic
models and the SQLAlchemy rows in ``clav.data.tables``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from clav.data import tables
from clav.domain.models import (
    Candle,
    EarningsEvent,
    Fill,
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


class TradeRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

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


class NewsItemRepository:
    """Persist + dedup + retention for news/filing items (Story 3.3)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def exists(self, content_hash: str) -> bool:
        return (
            self._session.scalar(
                select(tables.NewsItemRow.id).where(
                    tables.NewsItemRow.content_hash == content_hash
                )
            )
            is not None
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
        self.positions = PositionRepository(session)
        self.portfolio_snapshots = PortfolioSnapshotRepository(session)
        self.trade_proposals = TradeProposalRepository(session)
        self.prompt_versions = PromptVersionRepository(session)
        self.news_items = NewsItemRepository(session)
        self.social_digests = SocialDigestRepository(session)
        self.system_control = SystemControlRepository(session)
        self.audit_log = AuditLogRepository(session)
