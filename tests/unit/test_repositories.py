from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from clav.data import tables
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import (
    Candle,
    EarningsEvent,
    Fill,
    IndicatorSet,
    OrderRequest,
    PortfolioSnapshot,
    Position,
    RiskDecision,
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

        repos.system_control.set("emergency_stop", "true", updated_at=NOW, updated_by="operator")
        assert repos.system_control.get("emergency_stop") == "true"

        repos.system_control.set("emergency_stop", "false", updated_at=NOW, updated_by="operator")
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

        repos.scan_cycles.create("cycle-1", started_at=NOW, mode="dryrun", trigger="scheduled")
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


def test_earnings_event_add_and_get_upcoming(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")

        repos.earnings_events.add(
            instrument.id,
            EarningsEvent(
                symbol="AAPL",
                event_type="quarterly",
                scheduled_at=datetime(2025, 6, 5, tzinfo=UTC),
                confirmed=True,
                source="seed",
            ),
        )
        repos.earnings_events.add(
            instrument.id,
            EarningsEvent(
                symbol="AAPL",
                event_type="quarterly",
                scheduled_at=datetime(2025, 1, 1, tzinfo=UTC),
                confirmed=False,
                source="seed",
            ),
        )

        upcoming = repos.earnings_events.get_upcoming(instrument.id, after=NOW)
        assert len(upcoming) == 1
        assert upcoming[0].scheduled_at == datetime(2025, 6, 5)


def test_risk_evaluation_add_and_get_by_decision_id(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        repos.scan_cycles.create("cycle-1", started_at=NOW, mode="dryrun", trigger="scheduled")

        from types import SimpleNamespace

        decision_id = repos.decisions.add(
            scan_cycle_id="cycle-1",
            instrument_id=instrument.id,
            decision=SimpleNamespace(
                action="BUY",
                raw_score=0.5,
                technical_score=0.5,
                llm_signal=0.0,
                portfolio_bias=0.0,
                target_qty=10,
                reasoning={},
            ),
            created_at=NOW,
        )

        assert repos.risk_evaluations.get_by_decision_id(decision_id) is None

        repos.risk_evaluations.add(
            decision_id,
            RiskDecision(approved=True, adjusted_qty=8, blocked_by=[], notes={"cap": "sector"}),
            evaluated_at=NOW,
        )

        row = repos.risk_evaluations.get_by_decision_id(decision_id)
        assert row is not None
        assert row.approved is True
        assert row.adjusted_qty == 8
        assert row.notes == {"cap": "sector"}


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


def _make_closed_trade(repos, tag: str, *, symbol: str = "AAPL") -> tables.Trade:
    """Epic 5 test helper: a closed trade ready for review, defaulting to
    ``review_status='pending'`` (Story 5.1)."""
    instrument = repos.instruments.get_or_create(symbol)
    entry_req = OrderRequest(
        client_order_id=f"clav-{tag}-{symbol}-buy", symbol=symbol, side="buy", qty=8
    )
    entry_order = repos.orders.create(
        instrument_id=instrument.id, decision_id=None, request=entry_req, submitted_at=NOW
    )
    trade = repos.trades.open_trade(
        instrument_id=instrument.id,
        entry_order_id=entry_order.id,
        entry_decision_id=None,
        qty=8,
        entry_price=190.0,
        opened_at=NOW,
    )
    exit_req = OrderRequest(
        client_order_id=f"clav-{tag}-{symbol}-sell", symbol=symbol, side="sell", qty=8
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
    return trade


def _insert_review(
    repos, trade_id: int, *, created_at: datetime, calibration: str = "calibrated", tags=None
) -> tables.TradeReviewRow:
    return repos.trade_reviews.insert(
        trade_id,
        created_at=created_at,
        model="gemini-1.5-flash",
        why_entered="thesis",
        supporting_info=["catalyst"],
        risks_at_entry=["earnings risk"],
        reasoning_correct=True,
        what_worked=["momentum"],
        misleading_signals=[],
        hindsight_view="would have held longer",
        improvements=["widen stop"],
        confidence_calibration=calibration,
        tags=tags if tags is not None else ["earnings"],
        raw_response={"request": "...", "response": "..."},
    )


def test_trade_review_insert_and_list_for_trade_is_append_only(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        trade = _make_closed_trade(repos, "c1")

        first = _insert_review(repos, trade.id, created_at=NOW)
        second = _insert_review(
            repos, trade.id, created_at=NOW.replace(hour=13), calibration="overconfident"
        )

        history = repos.trade_reviews.list_for_trade(trade.id)
        assert [row.id for row in history] == [second.id, first.id]
        assert history[0].confidence_calibration == "overconfident"
        # the first review is still there -- append-only, not overwritten
        assert history[1].confidence_calibration == "calibrated"


def test_list_pending_reviews_excludes_reviewed_and_failed_trades(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        pending = _make_closed_trade(repos, "c1", symbol="AAPL")
        reviewed = _make_closed_trade(repos, "c2", symbol="MSFT")
        failed = _make_closed_trade(repos, "c3", symbol="TSLA")
        still_open_instrument = repos.instruments.get_or_create("NVDA")
        open_req = OrderRequest(
            client_order_id="clav-c4-NVDA-buy", symbol="NVDA", side="buy", qty=5
        )
        open_order = repos.orders.create(
            instrument_id=still_open_instrument.id,
            decision_id=None,
            request=open_req,
            submitted_at=NOW,
        )
        repos.trades.open_trade(
            instrument_id=still_open_instrument.id,
            entry_order_id=open_order.id,
            entry_decision_id=None,
            qty=5,
            entry_price=50.0,
            opened_at=NOW,
        )

        session.get(tables.Trade, reviewed.id).review_status = "reviewed"
        session.get(tables.Trade, failed.id).review_status = "failed"
        session.flush()

        result = repos.trades.list_pending_reviews(now=NOW, limit=50)
        assert [t.id for t in result] == [pending.id]


def test_list_pending_reviews_respects_backoff(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        trade = _make_closed_trade(repos, "c1", symbol="AAPL")
        repos.trades.mark_review_attempt_failed(
            trade.id, attempts=1, next_attempt_at=NOW.replace(hour=13)
        )

        # Still backing off: not yet eligible.
        assert repos.trades.list_pending_reviews(now=NOW, limit=50) == []
        # Backoff elapsed: eligible again, attempts/status preserved.
        result = repos.trades.list_pending_reviews(now=NOW.replace(hour=14), limit=50)
        assert [t.id for t in result] == [trade.id]
        assert result[0].review_attempts == 1
        assert result[0].review_status == "pending"


def test_mark_reviewed_and_mark_review_failed(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        reviewed_trade = _make_closed_trade(repos, "c1", symbol="AAPL")
        failed_trade = _make_closed_trade(repos, "c2", symbol="MSFT")

        repos.trades.mark_reviewed(reviewed_trade.id)
        row = session.get(tables.Trade, reviewed_trade.id)
        assert row.review_status == "reviewed"

        repos.trades.mark_review_failed(failed_trade.id, attempts=5)
        row = session.get(tables.Trade, failed_trade.id)
        assert row.review_status == "failed"
        assert row.review_attempts == 5
        # A failed trade is excluded from every future pass.
        assert repos.trades.list_pending_reviews(now=NOW.replace(hour=23), limit=50) == []


def test_trade_review_list_recent_filters_by_symbol_and_calibration(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        aapl_trade = _make_closed_trade(repos, "c1", symbol="AAPL")
        msft_trade = _make_closed_trade(repos, "c2", symbol="MSFT")
        _insert_review(repos, aapl_trade.id, created_at=NOW, calibration="calibrated")
        _insert_review(
            repos, msft_trade.id, created_at=NOW.replace(hour=13), calibration="overconfident"
        )

        by_symbol = repos.trade_reviews.list_recent(symbol="AAPL")
        assert [r.trade_id for r in by_symbol] == [aapl_trade.id]

        by_calibration = repos.trade_reviews.list_recent(calibration="overconfident")
        assert [r.trade_id for r in by_calibration] == [msft_trade.id]


def test_trade_review_aggregation_helpers(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        t1 = _make_closed_trade(repos, "c1", symbol="AAPL")
        t2 = _make_closed_trade(repos, "c2", symbol="MSFT")
        _insert_review(
            repos, t1.id, created_at=NOW, calibration="calibrated", tags=["earnings", "momentum"]
        )
        _insert_review(
            repos,
            t2.id,
            created_at=NOW.replace(hour=13),
            calibration="calibrated",
            tags=["earnings"],
        )

        assert repos.trade_reviews.calibration_verdict_counts() == {"calibrated": 2}
        assert repos.trade_reviews.tag_frequency() == {"earnings": 2, "momentum": 1}
