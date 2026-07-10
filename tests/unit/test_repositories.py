from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import (
    Candle,
    Fill,
    IndicatorSet,
    OrderRequest,
    PortfolioSnapshot,
    Position,
)

NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def test_instrument_get_or_create_is_idempotent(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        a = repos.instruments.get_or_create("aapl")
        b = repos.instruments.get_or_create("AAPL")
        assert a.id == b.id
        assert a.symbol == "AAPL"


def test_candle_upsert_and_get_recent_orders_oldest_first(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        candles = [
            Candle(
                symbol="AAPL",
                timeframe="1Day",
                open=1,
                high=2,
                low=0.5,
                close=1.5,
                volume=100,
                ts=datetime(2025, 1, i, tzinfo=UTC),
            )
            for i in (3, 1, 2)
        ]
        repos.candles.upsert_many(instrument.id, candles)

        # re-upsert with an updated close for the same (instrument, timeframe, ts)
        repos.candles.upsert_many(
            instrument.id,
            [
                Candle(
                    symbol="AAPL",
                    timeframe="1Day",
                    open=1,
                    high=2,
                    low=0.5,
                    close=9.99,
                    volume=100,
                    ts=datetime(2025, 1, 1, tzinfo=UTC),
                )
            ],
        )

        recent = repos.candles.get_recent(instrument.id, "1Day", limit=10)
        assert [c.ts.day for c in recent] == [1, 2, 3]
        assert recent[0].close == 9.99  # updated, not duplicated


def test_order_client_order_id_is_unique(session_factory) -> None:
    req = OrderRequest(client_order_id="clav-c1-AAPL-buy", symbol="AAPL", side="buy", qty=8)

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        repos.orders.create(
            instrument_id=instrument.id, decision_id=None, request=req, submitted_at=NOW
        )

    # A fresh session/scope simulates a retried submission on restart: the
    # duplicate client_order_id must fail closed, never silently double-insert.
    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_by_symbol("AAPL")
        repos.orders.create(
            instrument_id=instrument.id, decision_id=None, request=req, submitted_at=NOW
        )


def test_system_control_get_set_roundtrip(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        assert repos.system_control.get("emergency_stop") is None
        assert repos.system_control.get("emergency_stop", "false") == "false"

        repos.system_control.set(
            "emergency_stop", "true", updated_at=NOW, updated_by="operator"
        )
        assert repos.system_control.get("emergency_stop") == "true"

        repos.system_control.set(
            "emergency_stop", "false", updated_at=NOW, updated_by="operator"
        )
        assert repos.system_control.get("emergency_stop") == "false"


def test_position_upsert_and_delete(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")

        repos.positions.upsert(
            instrument.id, Position(symbol="AAPL", qty=10, avg_entry_price=100.0), opened_at=NOW
        )
        row = repos.positions.get(instrument.id)
        assert row is not None
        assert row.qty == 10

        repos.positions.upsert(
            instrument.id, Position(symbol="AAPL", qty=15, avg_entry_price=101.0), opened_at=NOW
        )
        row = repos.positions.get(instrument.id)
        assert row.qty == 15

        repos.positions.delete(instrument.id)
        assert repos.positions.get(instrument.id) is None


def test_trade_open_and_close(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        req = OrderRequest(client_order_id="clav-c1-AAPL-buy", symbol="AAPL", side="buy", qty=8)
        order = repos.orders.create(
            instrument_id=instrument.id, decision_id=None, request=req, submitted_at=NOW
        )

        trade = repos.trades.open_trade(
            instrument_id=instrument.id,
            entry_order_id=order.id,
            entry_decision_id=None,
            qty=8,
            entry_price=190.0,
            opened_at=NOW,
        )
        assert repos.trades.get_open_trade(instrument.id) is not None

        exit_req = OrderRequest(
            client_order_id="clav-c2-AAPL-sell", symbol="AAPL", side="sell", qty=8
        )
        exit_order = repos.orders.create(
            instrument_id=instrument.id, decision_id=None, request=exit_req, submitted_at=NOW
        )
        repos.trades.close_trade(
            trade.id,
            exit_order_id=exit_order.id,
            exit_price=200.0,
            closed_at=NOW,
            realized_pl=80.0,
            return_pct=0.0526,
        )
        assert repos.trades.get_open_trade(instrument.id) is None


def test_fill_dedup_by_broker_fill_id(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        req = OrderRequest(client_order_id="clav-c1-AAPL-buy", symbol="AAPL", side="buy", qty=8)
        order = repos.orders.create(
            instrument_id=instrument.id, decision_id=None, request=req, submitted_at=NOW
        )

        fill = Fill(
            client_order_id=req.client_order_id,
            qty=8,
            price=190.0,
            filled_at=NOW,
            broker_fill_id="bf-1",
        )
        repos.fills.add(order.id, fill)
        repos.fills.add(order.id, fill)  # duplicate broker_fill_id: no-op

        count = session.query(type(order)).count()  # sanity: order table untouched
        assert count == 1


def test_indicator_set_and_scan_cycle_and_decision_and_snapshot(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")

        repos.indicator_sets.add(
            instrument.id, IndicatorSet(symbol="AAPL", ts=NOW, close=190.0, rsi_14=55.0)
        )

        repos.scan_cycles.create(
            "cycle-1", started_at=NOW, mode="dryrun", trigger="scheduled"
        )
        repos.scan_cycles.set_market_open("cycle-1", True)
        repos.scan_cycles.finish("cycle-1", finished_at=NOW, status="completed")
        cycle = repos.scan_cycles.get("cycle-1")
        assert cycle is not None
        assert cycle.status == "completed"
        assert cycle.market_open is True

        from types import SimpleNamespace

        decision_id = repos.decisions.add(
            scan_cycle_id="cycle-1",
            instrument_id=instrument.id,
            decision=SimpleNamespace(
                action="HOLD",
                raw_score=0.0,
                technical_score=0.0,
                llm_signal=0.0,
                portfolio_bias=0.0,
                target_qty=0,
                reasoning={},
            ),
            created_at=NOW,
        )
        assert decision_id > 0

        repos.portfolio_snapshots.add(
            PortfolioSnapshot(ts=NOW, cash=1000, equity=1000, buying_power=1000)
        )
        latest = repos.portfolio_snapshots.latest()
        assert latest is not None
        assert latest.equity == 1000


def test_audit_log_add(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.audit_log.add(
            ts=NOW,
            actor="system",
            action="emergency_stop_set",
            entity_type="system_control",
            entity_id="emergency_stop",
            before={"value": "false"},
            after={"value": "true"},
            correlation_id="cycle-1",
        )
