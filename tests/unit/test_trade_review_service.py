"""Story 5.3 — TradeReviewService.build_context() assembles the full
provenance chain for one closed trade into a ReviewContext, read-only and
bounded, degrading gracefully on partial provenance.

Story 5.4 — run_pass() reviews every eligible trade under the shared
GeminiBudget/breaker, deferring on budget/breaker exhaustion (no attempt
counted) and retrying-then-terminally-failing a genuine ReviewError with
exponential backoff, all without ever touching the trading loop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import pytest

from clav.clock import FakeClock
from clav.data import tables
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import Candle, NewsItem, OrderRequest, RiskDecision, SocialDigest
from clav.integrations.llm.budget import LLMBudgetExceeded
from clav.integrations.llm.client import LLMResult
from clav.integrations.llm.provenance import ReviewCapture
from clav.interfaces.analyst import Analyst, ReviewError, TradeReview
from clav.services.review import DEFAULT_MAX_CANDLES, DEFAULT_MAX_NEWS_ITEMS, TradeReviewService

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


class _NullAnalyst(Analyst):
    """build_context() never calls the analyst -- this is just a type-correct
    placeholder for the constructor's required collaborator."""

    def analyze(self, symbol, news, social_digest, context):
        raise NotImplementedError

    def review(self, trade, context):
        raise NotImplementedError


def _service(factory) -> TradeReviewService:
    return TradeReviewService(analyst=_NullAnalyst(), session_factory=factory, clock=FakeClock(NOW))


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

        context = _service(session_factory).build_context(trade, repos)

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

        context = _service(session_factory).build_context(trade, repos)
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

        context = _service(session_factory).build_context(trade, repos)
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

        context = _service(session_factory).build_context(trade, repos)
        assert context.entry_action == "BUY"
        assert context.is_fallback is True
        assert context.entry_rationale == {}
        assert context.exit_reason == "unknown"


def test_open_trade_has_empty_price_path_and_unknown_exit_reason(session_factory) -> None:
    """build_context is only ever called on closed trades in practice
    (list_pending_reviews filters on status='closed'), but must still
    degrade gracefully rather than raise if handed a still-open one:
    no exit_order_id yet, no closed_at yet."""
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        entry_req = OrderRequest(
            client_order_id="clav-entry-open", symbol="AAPL", side="buy", qty=8
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

        context = _service(session_factory).build_context(trade, repos)
        assert context.price_path == []
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

        context = _service(session_factory).build_context(trade, repos)
        assert len(context.news_headlines) == DEFAULT_MAX_NEWS_ITEMS
        assert len(context.price_path) == DEFAULT_MAX_CANDLES


class FakeAnalyst(Analyst):
    """Consumes queued outcomes (a TradeReview or an exception to raise) in
    call order -- one per `review()` invocation, mirroring the order
    `list_pending_reviews` hands trades to `run_pass()` (oldest-closed-first)."""

    def __init__(self, outcomes: list) -> None:
        self._outcomes = list(outcomes)
        self.review_calls: list[int] = []

    def analyze(self, symbol, news, social_digest, context):
        raise NotImplementedError

    def review(self, trade, context) -> TradeReview:
        self.review_calls.append(trade.id)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _valid_review(*, tags=None) -> TradeReview:
    return TradeReview(
        why_entered="Strong earnings beat",
        supporting_info=["earnings beat"],
        risks_at_entry=["valuation"],
        reasoning_correct=True,
        what_worked=["momentum"],
        misleading_signals=[],
        hindsight_view="would have held longer",
        improvements=["widen stop"],
        confidence_calibration="calibrated",
        tags=tags if tags is not None else ["earnings"],
        model="fake",
    )


def _seed_closed_trade(repos, *, symbol: str, cycle_id: str, closed_at) -> tables.Trade:
    instrument = repos.instruments.get_or_create(symbol)
    entry_decision_id = _decision_id(
        repos, instrument.id, action="BUY", cycle_id=cycle_id, reasoning={}
    )
    exit_decision_id = _decision_id(
        repos, instrument.id, action="SELL", cycle_id=cycle_id, reasoning={}
    )
    return _open_and_close_trade(
        repos,
        instrument,
        entry_decision_id=entry_decision_id,
        exit_decision_id=exit_decision_id,
        closed_at=closed_at,
    )


def test_run_pass_reviews_every_pending_trade(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.scan_cycles.create("cycle-1", started_at=NOW, mode="paper", trigger="scheduled")
        t1 = _seed_closed_trade(
            repos, symbol="AAPL", cycle_id="cycle-1", closed_at=NOW + timedelta(days=1)
        )
        t2 = _seed_closed_trade(
            repos, symbol="MSFT", cycle_id="cycle-1", closed_at=NOW + timedelta(days=1)
        )
        t1_id, t2_id = t1.id, t2.id

    analyst = FakeAnalyst([_valid_review(), _valid_review(tags=["news-fade"])])
    service = TradeReviewService(
        analyst=analyst, session_factory=session_factory, clock=FakeClock(NOW)
    )
    service.run_pass()

    assert len(analyst.review_calls) == 2
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        for trade_id in (t1_id, t2_id):
            row = session.get(tables.Trade, trade_id)
            assert row.review_status == "reviewed"
            history = repos.trade_reviews.list_for_trade(trade_id)
            assert len(history) == 1
        tagged = repos.trade_reviews.list_for_trade(t2_id)[0]
        assert tagged.tags == ["news-fade"]


def test_budget_exhausted_defers_without_counting_attempt(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.scan_cycles.create("cycle-1", started_at=NOW, mode="paper", trigger="scheduled")
        trade = _seed_closed_trade(
            repos, symbol="AAPL", cycle_id="cycle-1", closed_at=NOW + timedelta(days=1)
        )
        trade_id = trade.id

    analyst = FakeAnalyst([LLMBudgetExceeded("daily budget spent")])
    service = TradeReviewService(
        analyst=analyst, session_factory=session_factory, clock=FakeClock(NOW)
    )
    service.run_pass()

    with session_scope(session_factory) as session:
        row = session.get(tables.Trade, trade_id)
        assert row.review_status == "pending"
        assert row.review_attempts == 0
        assert row.review_next_attempt_at is None


def test_repeated_review_error_reaches_max_attempts_and_terminally_fails(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.scan_cycles.create("cycle-1", started_at=NOW, mode="paper", trigger="scheduled")
        trade = _seed_closed_trade(
            repos, symbol="AAPL", cycle_id="cycle-1", closed_at=NOW + timedelta(days=1)
        )
        trade_id = trade.id

    clock = FakeClock(NOW)
    analyst = FakeAnalyst([ReviewError("boom")] * 3)
    service = TradeReviewService(
        analyst=analyst,
        session_factory=session_factory,
        clock=clock,
        max_attempts=3,
        backoff_base_seconds=60.0,
        backoff_max_seconds=60.0,
    )

    for _ in range(3):
        service.run_pass()
        clock.set(clock.now() + timedelta(minutes=5))  # past the 60s backoff each time

    assert len(analyst.review_calls) == 3
    with session_scope(session_factory) as session:
        row = session.get(tables.Trade, trade_id)
        assert row.review_status == "failed"
        assert row.review_attempts == 3
        assert len(Repositories(session).trade_reviews.list_for_trade(trade_id)) == 0


def test_one_trade_exception_does_not_halt_the_pass(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.scan_cycles.create("cycle-1", started_at=NOW, mode="paper", trigger="scheduled")
        broken = _seed_closed_trade(
            repos, symbol="AAPL", cycle_id="cycle-1", closed_at=NOW + timedelta(days=1)
        )
        healthy = _seed_closed_trade(
            repos, symbol="MSFT", cycle_id="cycle-1", closed_at=NOW + timedelta(days=1)
        )
        broken_id, healthy_id = broken.id, healthy.id

    analyst = FakeAnalyst([RuntimeError("unexpected"), _valid_review()])
    service = TradeReviewService(
        analyst=analyst, session_factory=session_factory, clock=FakeClock(NOW)
    )
    service.run_pass()  # must not raise

    assert len(analyst.review_calls) == 2
    with session_scope(session_factory) as session:
        broken_row = session.get(tables.Trade, broken_id)
        healthy_row = session.get(tables.Trade, healthy_id)
        # the unexpected error still counts as a failed attempt (defensive
        # path), the other trade in the same pass reviews normally
        assert broken_row.review_status == "pending"
        assert broken_row.review_attempts == 1
        assert healthy_row.review_status == "reviewed"


def test_run_pass_is_a_no_op_with_no_pending_trades(session_factory) -> None:
    analyst = FakeAnalyst([])
    service = TradeReviewService(
        analyst=analyst, session_factory=session_factory, clock=FakeClock(NOW)
    )
    service.run_pass()  # must not raise
    assert analyst.review_calls == []


def test_run_pass_drains_review_capture_into_raw_response(session_factory) -> None:
    """A successful review's redacted request/response (Story 5.2's
    ReviewProvenanceSink, buffered by ReviewCapture) lands in
    trade_review.raw_response -- the same provenance-capture pattern as
    Epic 3's AnalysisCapture. This fake analyst stands in for GeminiAnalyst,
    manually calling capture.record() the same way GeminiAnalyst.review()
    does internally after a successful call."""
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.scan_cycles.create("cycle-1", started_at=NOW, mode="paper", trigger="scheduled")
        trade = _seed_closed_trade(
            repos, symbol="AAPL", cycle_id="cycle-1", closed_at=NOW + timedelta(days=1)
        )
        trade_id = trade.id

    capture = ReviewCapture()

    class _CapturingAnalyst(Analyst):
        def analyze(self, symbol, news, social_digest, context):
            raise NotImplementedError

        def review(self, trade, context) -> TradeReview:
            review = _valid_review()
            capture.record(
                trade.id,
                "the redacted prompt",
                '{"why_entered": "..."}',
                review,
                LLMResult(text="...", prompt_tokens=10, completion_tokens=5, model="fake"),
            )
            return review

    service = TradeReviewService(
        analyst=_CapturingAnalyst(),
        session_factory=session_factory,
        clock=FakeClock(NOW),
        review_capture=capture,
    )
    service.run_pass()

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        review_row = repos.trade_reviews.list_for_trade(trade_id)[0]
        assert review_row.raw_response == {
            "request": "the redacted prompt",
            "response": '{"why_entered": "..."}',
        }


def test_context_build_failure_counts_as_a_failed_attempt(session_factory) -> None:
    """A context-assembly exception (defensive -- build_context is designed
    to degrade gracefully, per Story 5.3) is still routed through the same
    retry/backoff/terminal-failure path as a genuine ReviewError, rather than
    being silently swallowed or left retrying forever at zero cost."""
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.scan_cycles.create("cycle-1", started_at=NOW, mode="paper", trigger="scheduled")
        trade = _seed_closed_trade(
            repos, symbol="AAPL", cycle_id="cycle-1", closed_at=NOW + timedelta(days=1)
        )
        trade_id = trade.id

    service = TradeReviewService(
        analyst=FakeAnalyst([]), session_factory=session_factory, clock=FakeClock(NOW)
    )

    def _boom(self, trade, repos):
        raise RuntimeError("context build exploded")

    with mock.patch.object(TradeReviewService, "build_context", _boom):
        service.run_pass()

    with session_scope(session_factory) as session:
        row = session.get(tables.Trade, trade_id)
        assert row.review_status == "pending"
        assert row.review_attempts == 1
