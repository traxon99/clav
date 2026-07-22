"""Story 4.6 — AI-explanation & confidence view: a list of recent decisions
with a compact confidence/fallback marker, and a detail page resolving the
full provenance chain (exact analysis_result request/response, news/social
inputs, risk outcome, and the resulting order/fill/trade) via the
decision.reasoning.llm back-links Epic 3 already persists."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from clav.clock import FakeClock
from clav.config import Settings
from clav.data.db import make_engine
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import (
    AnalysisResult,
    Fill,
    NewsItem,
    OrderRequest,
    RiskDecision,
    SocialDigest,
    TradeDecision,
)
from clav.web.main import create_app

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _settings(tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        watchlist=["AAPL", "MSFT"],
        alpaca={"api_key": "k", "api_secret": "s"},
        data_dir=tmp_path,
    )


@pytest.fixture
def app_and_factory(tmp_path):
    cfg = _settings(tmp_path)
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))
    clock = FakeClock(NOW)
    app = create_app(cfg, clock=clock)
    return app, app.state.session_factory


def _seed_decision(
    factory,
    *,
    symbol: str = "AAPL",
    action: str = "BUY",
    cycle_id: str = "c1",
    llm: dict | None = None,
) -> int:
    """A bare decision row, with or without an ``llm`` reasoning block."""
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create(symbol)
    repos.scan_cycles.create(cycle_id, started_at=NOW, mode="dryrun", trigger="scheduled")
    decision = TradeDecision(
        cycle_id=cycle_id,
        symbol=symbol,
        action=action,  # type: ignore[arg-type]
        target_qty=10,
        raw_score=0.5,
        technical_score=0.3,
        llm_signal=0.4 if llm else 0.0,
        portfolio_bias=0.0,
        reasoning={"llm": llm} if llm else {},
    )
    decision_id = repos.decisions.add(
        scan_cycle_id=cycle_id, instrument_id=instrument.id, decision=decision, created_at=NOW
    )
    session.commit()
    session.close()
    return decision_id


def _seed_full_chain(factory) -> int:
    """A decision with the complete provenance chain: analysis_result, news,
    social digest, risk evaluation, order, fill, trade, and a trade_proposal
    (decision-journal entry)."""
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create("AAPL")
    repos.scan_cycles.create("c1", started_at=NOW, mode="dryrun", trigger="scheduled")

    analysis_result_id = repos.analysis_results.add(
        instrument.id,
        AnalysisResult(
            symbol="AAPL",
            model="gemini-1.5-flash",
            prompt_version="v1",
            sentiment=0.8,
            conviction=0.7,
            is_fallback=False,
            prompt_tokens=120,
            completion_tokens=30,
            request="REDACTED_REQUEST_PROMPT",
            response='{"sentiment": 0.8, "conviction": 0.7}',
            created_at=NOW,
        ),
    )
    repos.news_items.add_many(
        instrument.id,
        [
            NewsItem(
                id="n1",
                symbol="AAPL",
                headline="Apple beats earnings, raises guidance",
                body="Record quarter.",
                url="https://example.com/aapl",
                source="rss",
                published_at=NOW,
                fetched_at=NOW,
            )
        ],
    )
    news_ids = repos.news_items.recent_ids_for_analysis(
        instrument.id, now=NOW, max_age_hours=999, limit=10
    )
    digest_id = repos.social_digests.add(
        instrument.id,
        SocialDigest(
            symbol="AAPL",
            qualifying_post_count=42,
            bull_count=30,
            bear_count=12,
            bull_bear_ratio=2.5,
            mention_volume=200,
            baseline_volume=80.0,
            volume_ratio=2.5,
            anomaly_flag=False,
            generated_at=NOW,
        ),
    )

    decision = TradeDecision(
        cycle_id="c1",
        symbol="AAPL",
        action="BUY",
        target_qty=10,
        raw_score=0.6,
        technical_score=0.3,
        llm_signal=0.56,
        portfolio_bias=0.0,
        reasoning={
            "llm": {
                "sentiment": 0.8,
                "conviction": 0.7,
                "rationale": "Strong quarter plus bullish retail mood.",
                "prompt_version": "v1",
                "model": "gemini-1.5-flash",
                "is_fallback": False,
                "news_item_ids": news_ids,
                "social_digest_id": digest_id,
                "analysis_result_id": analysis_result_id,
            }
        },
    )
    decision_id = repos.decisions.add(
        scan_cycle_id="c1", instrument_id=instrument.id, decision=decision, created_at=NOW
    )
    repos.risk_evaluations.add(
        decision_id, RiskDecision(approved=True, adjusted_qty=10), evaluated_at=NOW
    )

    order_row = repos.orders.create(
        instrument_id=instrument.id,
        decision_id=decision_id,
        request=OrderRequest(client_order_id="clav-c1-AAPL-buy", symbol="AAPL", side="buy", qty=10),
        submitted_at=NOW,
    )
    repos.fills.add(
        order_row.id, Fill(client_order_id="clav-c1-AAPL-buy", qty=10, price=180.0, filled_at=NOW)
    )

    trade_row = repos.trades.open_trade(
        instrument_id=instrument.id,
        entry_order_id=order_row.id,
        entry_decision_id=decision_id,
        qty=10,
        entry_price=180.0,
        opened_at=NOW,
    )
    repos.trades.close_trade(
        trade_row.id,
        exit_order_id=order_row.id,
        exit_price=195.0,
        closed_at=NOW,
        realized_pl=150.0,
        return_pct=0.0833,
    )

    proposal = repos.trade_proposals.create(
        decision_id=decision_id,
        symbol="AAPL",
        side="buy",
        proposed_qty=10,
        rationale="Strong quarter plus bullish retail mood.",
        inputs_ref={
            "news_item_ids": news_ids,
            "social_digest_id": digest_id,
            "analysis_result_id": analysis_result_id,
        },
        status="executed",
        created_at=NOW,
        executed_qty=10,
    )
    session.commit()
    session.close()
    return decision_id, proposal.id  # type: ignore[return-value]


# --- list view ---------------------------------------------------------


def test_list_shows_conviction_and_gemini_badge(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_decision(
        factory,
        llm={
            "conviction": 0.65,
            "is_fallback": False,
            "sentiment": 0.5,
            "model": "gemini-1.5-flash",
        },
    )
    resp = TestClient(app).get("/explanations")
    assert resp.status_code == 200
    # Conviction is shown in plain language now (a word + percent), not a raw float.
    assert "65%" in resp.text
    assert "Medium" in resp.text
    assert "badge ok" in resp.text
    assert "gemini" in resp.text


def test_list_marks_fallback_decisions(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_decision(factory, llm={"conviction": 0.0, "is_fallback": True, "sentiment": 0.0})
    resp = TestClient(app).get("/explanations")
    assert "fallback" in resp.text
    assert "badge warn" in resp.text


def test_list_marks_technical_only_decisions(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_decision(factory, llm=None)
    resp = TestClient(app).get("/explanations")
    assert "technical-only" in resp.text
    assert "—" in resp.text  # no conviction to show


def test_list_filters_by_symbol(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_decision(factory, symbol="AAPL", cycle_id="c1")
    _seed_decision(factory, symbol="MSFT", cycle_id="c2")

    resp = TestClient(app).get("/explanations?symbol=MSFT")
    assert "of MSFT" in resp.text
    assert "of AAPL" not in resp.text


def test_list_filters_by_action(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_decision(factory, action="BUY", cycle_id="c1")
    _seed_decision(factory, action="SELL", cycle_id="c2")

    resp = TestClient(app).get("/explanations?action=SELL")
    body = resp.text
    # the plain-language feed shows the SELL and hides the BUY (these
    # headline phrases are unambiguous — unlike the filter dropdown's
    # "Buys"/"Sells" labels, which are always present).
    assert "Wants to sell" in body
    assert "Wants to buy" not in body


def test_list_pagination_round_trips(app_and_factory) -> None:
    app, factory = app_and_factory
    for i in range(5):
        _seed_decision(factory, cycle_id=f"c{i}")

    resp = TestClient(app).get("/explanations?limit=2")
    assert resp.status_code == 200
    assert "Older" in resp.text
    assert "offset=2" in resp.text

    resp2 = TestClient(app).get("/explanations?limit=2&offset=2")
    assert resp2.status_code == 200
    assert "Newer" in resp2.text


def test_list_empty_renders_gracefully(app_and_factory) -> None:
    app, _ = app_and_factory
    resp = TestClient(app).get("/explanations")
    assert resp.status_code == 200
    assert "No decisions yet." in resp.text


# --- detail view ---------------------------------------------------------


def test_detail_renders_full_provenance_chain(app_and_factory) -> None:
    app, factory = app_and_factory
    decision_id, proposal_id = _seed_full_chain(factory)

    resp = TestClient(app).get(f"/explanations/{decision_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "REDACTED_REQUEST_PROMPT" in body
    assert "sentiment" in body and "0.7" in body  # the raw (HTML-escaped) response JSON
    assert "gemini-1.5-flash" in body
    assert "Apple beats earnings, raises guidance" in body
    assert "Strong quarter plus bullish retail mood." in body
    # social digest
    assert "42" in body  # qualifying_post_count
    # risk outcome
    assert "Approved" in body
    # order/fill/trade
    assert "150.00" in body  # realized P/L
    assert f"/journal/{proposal_id}" in body


def test_detail_technical_only_decision_shows_no_llm_message(app_and_factory) -> None:
    app, factory = app_and_factory
    decision_id = _seed_decision(factory, llm=None)

    resp = TestClient(app).get(f"/explanations/{decision_id}")
    assert resp.status_code == 200
    assert "no LLM signal fed this decision" in resp.text


def test_detail_unknown_decision_returns_404(app_and_factory) -> None:
    app, _ = app_and_factory
    resp = TestClient(app).get("/explanations/999999")
    assert resp.status_code == 404
    assert "Not found" in resp.text
