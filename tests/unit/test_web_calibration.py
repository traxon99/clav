"""Story 4.9 — GET /calibration: renders the descriptive conviction-vs-
outcome view (scatter + bucketed summary tables) end to end through the
FastAPI route, including the empty-DB state.

Story 5.6 — the same page's second panel: a confidence_calibration verdict
breakdown against realized outcome, plus tag/misleading-signal frequency,
sourced from the trade_review journal instead of decision."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from clav.clock import FakeClock
from clav.config import Settings
from clav.data.db import make_engine
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import OrderRequest, TradeDecision
from clav.web.main import create_app

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _settings(tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        watchlist=["AAPL"],
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


def test_empty_history_renders_gracefully(app_and_factory) -> None:
    app, _ = app_and_factory
    resp = TestClient(app).get("/calibration")
    assert resp.status_code == 200
    assert "not enough data yet" in resp.text
    assert "0 closed trades" in resp.text
    # Story 5.6's review-journal panel, empty state.
    assert "No trade reviews yet." in resp.text
    assert "No tags recorded yet." in resp.text
    assert "No misleading signals recorded yet." in resp.text


def test_renders_seeded_gemini_and_technical_trades(app_and_factory) -> None:
    app, factory = app_and_factory
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create("AAPL")
    repos.scan_cycles.create("c1", started_at=NOW, mode="dryrun", trigger="scheduled")

    decision = TradeDecision(
        cycle_id="c1",
        symbol="AAPL",
        action="BUY",
        target_qty=10,
        raw_score=0.5,
        technical_score=0.3,
        llm_signal=0.8,
        portfolio_bias=0.0,
        reasoning={"llm": {"conviction": 0.8, "sentiment": 0.6}},
    )
    decision_id = repos.decisions.add(
        scan_cycle_id="c1", instrument_id=instrument.id, decision=decision, created_at=NOW
    )
    order_row = repos.orders.create(
        instrument_id=instrument.id,
        decision_id=decision_id,
        request=OrderRequest(client_order_id="clav-c1-AAPL-buy", symbol="AAPL", side="buy", qty=10),
        submitted_at=NOW,
    )
    trade_row = repos.trades.open_trade(
        instrument_id=instrument.id,
        entry_order_id=order_row.id,
        entry_decision_id=decision_id,
        qty=10,
        entry_price=100.0,
        opened_at=NOW,
    )
    repos.trades.close_trade(
        trade_row.id,
        exit_order_id=order_row.id,
        exit_price=115.0,
        closed_at=NOW,
        realized_pl=150.0,
        return_pct=0.15,
    )
    session.commit()
    session.close()

    resp = TestClient(app).get("/calibration")
    assert resp.status_code == 200
    body = resp.text
    assert "1 closed trade" in body
    assert "1 Gemini-driven" in body
    assert "0 technical-only" in body
    assert "<circle" in body
    assert "15.00%" in body
    assert "100%" in body  # hit rate


# --- Story 5.6: review-journal verdict/tag panel ----------------------------


def _seed_closed_trade_with_review(
    factory,
    *,
    symbol: str,
    tag: str,
    calibration: str,
    realized_pl: float,
    return_pct: float,
    review_tags: list[str] | None = None,
    misleading_signals: list[str] | None = None,
) -> None:
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create(symbol)

    entry_req = OrderRequest(client_order_id=f"clav-entry-{tag}", symbol=symbol, side="buy", qty=8)
    entry_order = repos.orders.create(
        instrument_id=instrument.id, decision_id=None, request=entry_req, submitted_at=NOW
    )
    trade = repos.trades.open_trade(
        instrument_id=instrument.id,
        entry_order_id=entry_order.id,
        entry_decision_id=None,
        qty=8,
        entry_price=100.0,
        opened_at=NOW,
    )
    exit_req = OrderRequest(client_order_id=f"clav-exit-{tag}", symbol=symbol, side="sell", qty=8)
    exit_order = repos.orders.create(
        instrument_id=instrument.id, decision_id=None, request=exit_req, submitted_at=NOW
    )
    repos.trades.close_trade(
        trade.id,
        exit_order_id=exit_order.id,
        exit_price=100.0 * (1 + return_pct),
        closed_at=NOW,
        realized_pl=realized_pl,
        return_pct=return_pct,
    )
    repos.trade_reviews.insert(
        trade.id,
        created_at=NOW,
        model="gemini-1.5-flash",
        why_entered="thesis",
        supporting_info=[],
        risks_at_entry=[],
        reasoning_correct=True,
        what_worked=[],
        misleading_signals=misleading_signals or [],
        hindsight_view="hindsight",
        improvements=[],
        confidence_calibration=calibration,
        tags=review_tags or [],
        raw_response={},
    )
    repos.trades.mark_reviewed(trade.id)
    session.commit()
    session.close()


def test_review_panel_verdict_breakdown_against_outcome(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_closed_trade_with_review(
        factory,
        symbol="AAPL",
        tag="a",
        calibration="overconfident",
        realized_pl=-50.0,
        return_pct=-0.10,
    )
    _seed_closed_trade_with_review(
        factory,
        symbol="MSFT",
        tag="b",
        calibration="calibrated",
        realized_pl=100.0,
        return_pct=0.20,
    )

    resp = TestClient(app).get("/calibration")
    assert resp.status_code == 200
    body = resp.text
    assert "overconfident" in body
    assert "calibrated" in body
    assert "-10.00%" in body  # overconfident's mean return
    assert "20.00%" in body  # calibrated's mean return
    assert "2 reviews" in body


def test_review_panel_tag_and_misleading_signal_frequency(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_closed_trade_with_review(
        factory,
        symbol="AAPL",
        tag="a",
        calibration="calibrated",
        realized_pl=10.0,
        return_pct=0.05,
        review_tags=["earnings", "momentum"],
        misleading_signals=["social hype"],
    )
    _seed_closed_trade_with_review(
        factory,
        symbol="MSFT",
        tag="b",
        calibration="calibrated",
        realized_pl=10.0,
        return_pct=0.05,
        review_tags=["earnings"],
        misleading_signals=["social hype"],
    )

    resp = TestClient(app).get("/calibration")
    assert resp.status_code == 200
    body = resp.text
    # "earnings" (2) must render before "momentum" (1) -- sorted by count desc.
    assert body.index("earnings") < body.index("momentum")
    assert "social hype" in body


def test_review_panel_small_sample_does_not_divide_by_zero(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_closed_trade_with_review(
        factory,
        symbol="AAPL",
        tag="a",
        calibration="overconfident",
        realized_pl=10.0,
        return_pct=0.05,
    )

    resp = TestClient(app).get("/calibration")
    assert resp.status_code == 200
    body = resp.text
    assert "1 review" in body
    # calibrated/underconfident buckets are empty -- rendered as "—", not a crash.
    assert "—" in body
