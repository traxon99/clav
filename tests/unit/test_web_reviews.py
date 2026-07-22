"""Story 5.5 — /reviews list + detail: every closed trade shows up (not just
ones with an existing trade_review row), pending/failed states render
clearly, a re-reviewed trade shows its full history newest-first, and the
detail page links back to the entry's /explanations/{decision_id}."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clav.clock import FakeClock
from clav.config import Settings
from clav.data.db import make_engine
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import OrderRequest
from clav.web.main import create_app

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


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


def _seed_closed_trade(
    factory,
    *,
    symbol: str = "AAPL",
    tag: str = "t1",
    with_entry_decision: bool = True,
) -> int:
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create(symbol)

    entry_decision_id = None
    if with_entry_decision:
        repos.scan_cycles.create(f"c-{tag}", started_at=NOW, mode="dryrun", trigger="scheduled")
        entry_decision_id = repos.decisions.add(
            scan_cycle_id=f"c-{tag}",
            instrument_id=instrument.id,
            decision=SimpleNamespace(
                action="BUY",
                raw_score=0.5,
                technical_score=0.4,
                llm_signal=0.6,
                portfolio_bias=0.0,
                target_qty=8,
                reasoning={},
            ),
            created_at=NOW,
        )

    entry_req = OrderRequest(client_order_id=f"clav-entry-{tag}", symbol=symbol, side="buy", qty=8)
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
    exit_req = OrderRequest(client_order_id=f"clav-exit-{tag}", symbol=symbol, side="sell", qty=8)
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
    trade_id = trade.id
    session.commit()
    session.close()
    return trade_id


def _insert_review(factory, trade_id: int, *, calibration: str = "calibrated", tags=None) -> None:
    session = factory()
    repos = Repositories(session)
    repos.trade_reviews.insert(
        trade_id,
        created_at=NOW,
        model="gemini-1.5-flash",
        why_entered="Strong earnings beat",
        supporting_info=["earnings beat"],
        risks_at_entry=["valuation"],
        reasoning_correct=True,
        what_worked=["momentum"],
        misleading_signals=[],
        hindsight_view="would have held longer",
        improvements=["widen stop"],
        confidence_calibration=calibration,
        tags=tags if tags is not None else ["earnings"],
        raw_response={"request": "req", "response": "resp"},
    )
    repos.trades.mark_reviewed(trade_id)
    session.commit()
    session.close()


def _mark_failed(factory, trade_id: int, *, attempts: int) -> None:
    session = factory()
    repos = Repositories(session)
    repos.trades.mark_review_failed(trade_id, attempts=attempts)
    session.commit()
    session.close()


# --- list view ---------------------------------------------------------


def test_list_shows_pending_trade_with_no_review_yet(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_closed_trade(factory, symbol="AAPL")

    resp = TestClient(app).get("/reviews")
    assert resp.status_code == 200
    assert "AAPL" in resp.text
    assert "badge warn" in resp.text
    assert "pending" in resp.text


def test_list_shows_failed_trade_with_attempt_count(app_and_factory) -> None:
    app, factory = app_and_factory
    trade_id = _seed_closed_trade(factory, symbol="MSFT")
    _mark_failed(factory, trade_id, attempts=5)

    resp = TestClient(app).get("/reviews")
    assert resp.status_code == 200
    assert "failed (5 attempts)" in resp.text


def test_list_shows_reviewed_trade_with_calibration_and_tags(app_and_factory) -> None:
    app, factory = app_and_factory
    trade_id = _seed_closed_trade(factory, symbol="AAPL")
    _insert_review(factory, trade_id, calibration="overconfident", tags=["earnings", "news-fade"])

    resp = TestClient(app).get("/reviews")
    assert resp.status_code == 200
    assert "badge ok" in resp.text
    assert "reviewed" in resp.text
    assert "overconfident" in resp.text
    assert "earnings, news-fade" in resp.text


def test_list_filters_by_symbol(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_closed_trade(factory, symbol="AAPL", tag="a")
    _seed_closed_trade(factory, symbol="MSFT", tag="b")

    resp = TestClient(app).get("/reviews?symbol=MSFT")
    assert "MSFT" in resp.text
    assert "AAPL" not in resp.text


def test_list_filters_by_tag(app_and_factory) -> None:
    app, factory = app_and_factory
    t1 = _seed_closed_trade(factory, symbol="AAPL", tag="a")
    t2 = _seed_closed_trade(factory, symbol="MSFT", tag="b")
    _insert_review(factory, t1, tags=["earnings"])
    _insert_review(factory, t2, tags=["news-fade"])

    resp = TestClient(app).get("/reviews?tag=news-fade")
    assert "MSFT" in resp.text
    assert "AAPL" not in resp.text


def test_list_filters_by_calibration(app_and_factory) -> None:
    app, factory = app_and_factory
    t1 = _seed_closed_trade(factory, symbol="AAPL", tag="a")
    t2 = _seed_closed_trade(factory, symbol="MSFT", tag="b")
    _insert_review(factory, t1, calibration="overconfident")
    _insert_review(factory, t2, calibration="calibrated")

    resp = TestClient(app).get("/reviews?calibration=calibrated")
    assert "MSFT" in resp.text
    assert "AAPL" not in resp.text


def test_list_pagination_round_trips(app_and_factory) -> None:
    app, factory = app_and_factory
    for i in range(5):
        _seed_closed_trade(factory, symbol="AAPL", tag=f"t{i}")

    resp = TestClient(app).get("/reviews?limit=2")
    assert resp.status_code == 200
    assert "Older" in resp.text
    assert "offset=2" in resp.text

    resp2 = TestClient(app).get("/reviews?limit=2&offset=2")
    assert resp2.status_code == 200
    assert "Newer" in resp2.text


def test_list_empty_renders_gracefully(app_and_factory) -> None:
    app, _ = app_and_factory
    resp = TestClient(app).get("/reviews")
    assert resp.status_code == 200
    assert "No closed trades yet." in resp.text


# --- detail view ---------------------------------------------------------


def test_detail_pending_trade_renders_clearly(app_and_factory) -> None:
    app, factory = app_and_factory
    trade_id = _seed_closed_trade(factory, symbol="AAPL")

    resp = TestClient(app).get(f"/reviews/{trade_id}")
    assert resp.status_code == 200
    assert "Review pending" in resp.text


def test_detail_failed_trade_renders_clearly(app_and_factory) -> None:
    app, factory = app_and_factory
    trade_id = _seed_closed_trade(factory, symbol="AAPL")
    _mark_failed(factory, trade_id, attempts=5)

    resp = TestClient(app).get(f"/reviews/{trade_id}")
    assert resp.status_code == 200
    assert "Review failed after 5 attempts" in resp.text


def test_detail_renders_full_review_and_links_to_explanation(app_and_factory) -> None:
    app, factory = app_and_factory
    trade_id = _seed_closed_trade(factory, symbol="AAPL")
    _insert_review(factory, trade_id)

    resp = TestClient(app).get(f"/reviews/{trade_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "Strong earnings beat" in body
    assert "earnings beat" in body
    assert "valuation" in body
    assert "momentum" in body
    assert "would have held longer" in body
    assert "widen stop" in body
    assert "gemini-1.5-flash" in body
    # entry_decision_id was seeded -- links back to Epic 4's explanation view
    session = factory()
    repos = Repositories(session)
    trade = repos.trades.get(trade_id)
    assert f"/explanations/{trade.entry_decision_id}" in body
    session.close()


def test_detail_re_reviewed_trade_shows_both_rows_newest_first(app_and_factory) -> None:
    app, factory = app_and_factory
    trade_id = _seed_closed_trade(factory, symbol="AAPL")
    _insert_review(factory, trade_id, calibration="overconfident", tags=["first"])
    _insert_review(factory, trade_id, calibration="calibrated", tags=["second"])

    resp = TestClient(app).get(f"/reviews/{trade_id}")
    assert resp.status_code == 200
    body = resp.text
    # newest (second) review appears before the oldest (first) one
    assert body.index("second") < body.index("first")
    assert "overconfident" in body
    assert "calibrated" in body


def test_detail_unknown_trade_returns_404(app_and_factory) -> None:
    app, _ = app_and_factory
    resp = TestClient(app).get("/reviews/999999")
    assert resp.status_code == 404
    assert "Not found" in resp.text


def test_detail_trade_with_no_entry_decision_has_no_explanation_link(app_and_factory) -> None:
    app, factory = app_and_factory
    trade_id = _seed_closed_trade(factory, symbol="AAPL", with_entry_decision=False)

    resp = TestClient(app).get(f"/reviews/{trade_id}")
    assert resp.status_code == 200
    assert "/explanations/" not in resp.text
