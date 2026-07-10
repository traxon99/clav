"""Repository classes — the only code in CLAV that issues SQL (docs/03-database.md,
docs/05-class-design.md §2). Each repository converts between domain Pydantic
models and the SQLAlchemy rows in ``clav.data.tables``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from clav.data import tables
from clav.domain.models import (
    Candle,
    Fill,
    IndicatorSet,
    Order,
    OrderRequest,
    PortfolioSnapshot,
)
from clav.domain.models import (
    Position as PositionModel,
)


class InstrumentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_or_create(self, symbol: str) -> tables.Instrument:
        symbol = symbol.upper()
        row = self._session.scalar(
            select(tables.Instrument).where(tables.Instrument.symbol == symbol)
        )
        if row is None:
            row = tables.Instrument(symbol=symbol)
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
                reconciled=snapshot.reconciled,
            )
        )
        self._session.flush()

    def latest(self) -> tables.PortfolioSnapshot | None:
        return self._session.scalar(
            select(tables.PortfolioSnapshot).order_by(tables.PortfolioSnapshot.ts.desc()).limit(1)
        )


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
        self.indicator_sets = IndicatorSetRepository(session)
        self.scan_cycles = ScanCycleRepository(session)
        self.decisions = DecisionRepository(session)
        self.orders = OrderRepository(session)
        self.fills = FillRepository(session)
        self.trades = TradeRepository(session)
        self.positions = PositionRepository(session)
        self.portfolio_snapshots = PortfolioSnapshotRepository(session)
        self.system_control = SystemControlRepository(session)
        self.audit_log = AuditLogRepository(session)
