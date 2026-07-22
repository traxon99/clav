"""Story 5.3 — TradeReviewService.build_context() assembles the full
provenance chain for one closed trade into a ReviewContext, read-only and
bounded, degrading gracefully on partial provenance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import Candle, NewsItem, OrderRequest, RiskDecision, SocialDigest
from clav.services.review import DEFAULT_MAX_CANDLES, DEFAULT_MAX_NEWS_ITEMS, TradeReviewService

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _decision_id(repos, instrument_id, *, action: str, reasoning: dict, cycle_id: str) -> int:
    return repos.decisions.add(
        scan_cycle_id=cycle_id,
        instrument_id=instrument_id,
        decision=SimpleNamespace(
            action=action,
            raw_score=0.5,
            technical_score=0.4,
            llm_signal=0.6,
            portfolio_bias=0.0,
            target_qty=8,
            reasoning=reasoning,
        ),
        created_at=NOW,
    )


def _open_and_close_trade(repos, instrument, *, entry_decision_id, exit_decision_id, closed_at):
    entry_req = OrderRequest(
        client_order_id=f"clav-entry-{instrument.symbol}",
        symbol=instrument.symbol,
        side="buy",
        qty=8,
    )
    entry_order = repos.orders.create(
        instrument_id=instrument.id,
        decision_id=entry_decision_id,
        request=entry_req,
        submitted_at=NOW,
    )
    trade = repos.trades.open_trade(
        instrument_id=instrument.id,
        entry_order_id=entry_order.id,
        entry_decision_id=entry_decision_id,
        qty=8,
        entry_price=190.0,
        opened_at=NOW,
    )
    exit_req = OrderRequest(
        client_order_id=f"clav-exit-{instrument.symbol}",
        symbol=instrument.symbol,
        side="sell",
        qty=8,
    )
    exit_order = repos.orders.create(
        instrument_id=instrument.id,
        decision_id=exit_decision_id,
        request=exit_req,
        submitted_at=closed_at,
    )
    repos.trades.close_trade(
        trade.id,
        exit_order_id=exit_order.id,
        exit_price=200.0,
        closed_at=closed_at,
        realized_pl=80.0,
        return_pct=0.0526,
    )
    return trade


def test_build_context_full_chain(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        repos.scan_cycles.create("cycle-1", started_at=NOW, mode="paper", trigger="scheduled")

        news = repos.news_items.add_many(
            instrument.id,
            [
                NewsItem(
                    id="n1",
                    symbol="AAPL",
                    headline="Apple beats earnings",
                    body="Record iPhone sales",
                    source="rss",
                    published_at=NOW,
                    fetched_at=NOW,
                )
            ],
        )
        news_item_ids = repos.news_items.recent_ids_for_analysis(
            instrument.id, now=NOW, max_age_hours=24, limit=10
        )
        assert len(news) == 1 and len(news_item_ids) == 1

        digest_id = repos.social_digests.add(
            instrument.id,
            SocialDigest(
                symbol="AAPL",
                qualifying_post_count=5,
                bull_count=4,
                bear_count=1,
                bull_bear_ratio=4.0,
                mention_volume=5,
                baseline_volume=2.0,
                volume_ratio=2.5,
                anomaly_flag=False,
                top_posts=[],
                generated_at=NOW,
            ),
        )

        entry_decision_id = _decision_id(
            repos,
            instrument.id,
            action="BUY",
            cycle_id="cycle-1",
            reasoning={
                "technical_score": 0.4,
                "llm": {
                    "sentiment": 0.8,
                    "conviction": 0.7,
                    "rationale": "Strong earnings beat",
                    "model": "gemini-1.5-flash",
                    "is_fallback": False,
                    "news_item_ids": news_item_ids,
                    "social_digest_id": digest_id,
                },
            },
        )
        repos.risk_evaluations.add(
            entry_decision_id,
            RiskDecision(
                approved=True,
                adjusted_qty=8,
                blocked_by=[],
                notes={"MaxPositionSizeRule": {"passed": True}},
            ),
            evaluated_at=NOW,
        )

        closed_at = NOW + timedelta(days=2)
        exit_decision_id = _decision_id(
            repos, instrument.id, action="SELL", cycle_id="cycle-1", reasoning={}
        )
        repos.risk_evaluations.add(
            exit_decision_id,
            RiskDecision(approved=True, adjusted_qty=8, blocked_by=[], notes={}),
            evaluated_at=closed_at,
        )

        repos.candles.upsert_many(
            instrument.id,
            [
                Candle(
                    symbol="AAPL",
                    timeframe="1Day",
                    open=185.0,
                    high=192.0,
                    low=184.0,
                    close=close,
                    volume=1_000_000,
                    ts=NOW + timedelta(days=i),
                )
                for i, close in enumerate([190.0, 195.0, 200.0])
            ],
        )

        trade = _open_and_close_trade(
            repos,
            instrument,
            entry_decision_id=entry_decision_id,
            exit_decision_id=exit_decision_id,
            closed_at=closed_at,
        )

        context = TradeReviewService().build_context(trade, repos)

        assert context.entry_action == "BUY"
        assert context.raw_score == 0.5
        assert context.technical_score == 0.4
        assert context.llm_signal == 0.6
        assert context.is_fallback is False
        assert context.entry_rationale["llm"]["rationale"] == "Strong earnings beat"
        assert context.risk_notes == {"MaxPositionSizeRule": {"passed": True}}
        assert context.news_headlines == ["Apple beats earnings"]
        assert "bull=4" in context.social_summary
        assert context.price_path == [190.0, 195.0, 200.0]
        assert context.exit_reason == "signal"


def test_stop_monitor_exit_derives_exit_reason(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        repos.scan_cycles.create("cycle-1", started_at=NOW, mode="paper", trigger="scheduled")

        entry_decision_id = _decision_id(
            repos, instrument.id, action="BUY", cycle_id="cycle-1", reasoning={}
        )
        repos.risk_evaluations.add(
            entry_decision_id,
            RiskDecision(approved=True, adjusted_qty=8, blocked_by=[], notes={}),
            evaluated_at=NOW,
        )

        closed_at = NOW + timedelta(days=1)
        exit_decision_id = _decision_id(
            repos,
            instrument.id,
            action="SELL",
            cycle_id="cycle-1",
            reasoning={"source": "stop_monitor", "trigger": "stop_loss"},
        )
        repos.risk_evaluations.add(
            exit_decision_id,
            RiskDecision(
                approved=True, adjusted_qty=8, blocked_by=[], notes={"source": "stop_monitor"}
            ),
            evaluated_at=closed_at,
        )

        trade = _open_and_close_trade(
            repos,
            instrument,
            entry_decision_id=entry_decision_id,
            exit_decision_id=exit_decision_id,
            closed_at=closed_at,
        )

        context = TradeReviewService().build_context(trade, repos)
        assert context.exit_reason == "stop_monitor"


def test_technical_only_entry_is_reflected(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        repos.scan_cycles.create("cycle-1", started_at=NOW, mode="paper", trigger="scheduled")

        # No "llm" key at all -- the analyst was never wired for this entry,
        # same as Epic 1/2's llm_signal=0 technical-only path.
        entry_decision_id = _decision_id(
            repos, instrument.id, action="BUY", cycle_id="cycle-1", reasoning={}
        )
        repos.risk_evaluations.add(
            entry_decision_id,
            RiskDecision(approved=True, adjusted_qty=8, blocked_by=[], notes={}),
            evaluated_at=NOW,
        )
        closed_at = NOW + timedelta(days=1)
        exit_decision_id = _decision_id(
            repos, instrument.id, action="SELL", cycle_id="cycle-1", reasoning={}
        )
        repos.risk_evaluations.add(
            exit_decision_id,
            RiskDecision(approved=True, adjusted_qty=8, blocked_by=[], notes={}),
            evaluated_at=closed_at,
        )
        trade = _open_and_close_trade(
            repos,
            instrument,
            entry_decision_id=entry_decision_id,
            exit_decision_id=exit_decision_id,
            closed_at=closed_at,
        )

        context = TradeReviewService().build_context(trade, repos)
        assert context.is_fallback is True
        assert context.news_headlines == []
        assert context.social_summary == ""


def test_missing_entry_decision_degrades_gracefully(session_factory) -> None:
    """A trade with no entry_decision_id (e.g. a very old/backfilled row)
    still yields a context, never raises."""
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        entry_req = OrderRequest(client_order_id="clav-entry-x", symbol="AAPL", side="buy", qty=8)
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
        repos.trades.close_trade(
            trade.id,
            exit_order_id=entry_order.id,
            exit_price=200.0,
            closed_at=NOW + timedelta(days=1),
            realized_pl=80.0,
            return_pct=0.0526,
        )

        context = TradeReviewService().build_context(trade, repos)
        assert context.entry_action == "BUY"
        assert context.is_fallback is True
        assert context.entry_rationale == {}
        assert context.exit_reason == "unknown"


def test_candle_and_news_pulls_are_bounded(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        repos.scan_cycles.create("cycle-1", started_at=NOW, mode="paper", trigger="scheduled")

        news_items = [
            NewsItem(
                id=f"n{i}",
                symbol="AAPL",
                headline=f"Headline {i}",
                source="rss",
                published_at=NOW,
                fetched_at=NOW,
            )
            for i in range(DEFAULT_MAX_NEWS_ITEMS + 5)
        ]
        repos.news_items.add_many(instrument.id, news_items)
        news_item_ids = repos.news_items.recent_ids_for_analysis(
            instrument.id, now=NOW, max_age_hours=24, limit=DEFAULT_MAX_NEWS_ITEMS + 5
        )
        assert len(news_item_ids) == DEFAULT_MAX_NEWS_ITEMS + 5

        closed_at = NOW + timedelta(days=DEFAULT_MAX_CANDLES + 10)
        repos.candles.upsert_many(
            instrument.id,
            [
                Candle(
                    symbol="AAPL",
                    timeframe="1Day",
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.0 + i,
                    volume=1000,
                    ts=NOW + timedelta(days=i),
                )
                for i in range(DEFAULT_MAX_CANDLES + 10)
            ],
        )

        entry_decision_id = _decision_id(
            repos,
            instrument.id,
            action="BUY",
            cycle_id="cycle-1",
            reasoning={"llm": {"is_fallback": False, "news_item_ids": news_item_ids}},
        )
        exit_decision_id = _decision_id(
            repos, instrument.id, action="SELL", cycle_id="cycle-1", reasoning={}
        )
        trade = _open_and_close_trade(
            repos,
            instrument,
            entry_decision_id=entry_decision_id,
            exit_decision_id=exit_decision_id,
            closed_at=closed_at,
        )

        context = TradeReviewService().build_context(trade, repos)
        assert len(context.news_headlines) == DEFAULT_MAX_NEWS_ITEMS
        assert len(context.price_path) == DEFAULT_MAX_CANDLES
