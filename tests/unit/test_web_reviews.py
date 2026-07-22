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
from clav.interfaces.analyst import Analyst, TradeReview
from clav.services.review import TradeReviewService
from clav.web.main import create_app

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _settings(tmp_path, *, token: str | None = None) -> Settings:
    overrides: dict = {
        "watchlist": ["AAPL", "MSFT"],
        "alpaca": {"api_key": "k", "api_secret": "s"},
        "data_dir": tmp_path,
    }
    if token is not None:
        overrides["web"] = {"token": token}
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


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


# --- Story 5.7: manual rerun ------------------------------------------------


def test_rerun_endpoint_requeues_a_failed_trade(app_and_factory) -> None:
    app, factory = app_and_factory
    trade_id = _seed_closed_trade(factory, symbol="AAPL")
    _insert_review(factory, trade_id, calibration="overconfident")
    _mark_failed(factory, trade_id, attempts=5)

    resp = TestClient(app).post(f"/api/reviews/{trade_id}/rerun")
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_status"] == "pending"
    assert body["review_attempts"] == 0

    session = factory()
    repos = Repositories(session)
    row = repos.trades.get(trade_id)
    assert row.review_status == "pending"
    assert row.review_attempts == 0
    assert row.review_next_attempt_at is None
    # the existing review is untouched, not deleted by the reset
    assert len(repos.trade_reviews.list_for_trade(trade_id)) == 1
    session.close()


def test_rerun_endpoint_404_for_unknown_trade(app_and_factory) -> None:
    app, _ = app_and_factory
    resp = TestClient(app).post("/api/reviews/999999/rerun")
    assert resp.status_code == 404


def test_rerun_endpoint_requires_token_when_configured(tmp_path) -> None:
    cfg = _settings(tmp_path, token="s3cret")
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))
    app = create_app(cfg, clock=FakeClock(NOW))
    factory = app.state.session_factory
    trade_id = _seed_closed_trade(factory, symbol="AAPL")

    client = TestClient(app)
    no_token = client.post(f"/api/reviews/{trade_id}/rerun")
    assert no_token.status_code == 401

    with_token = client.post(f"/api/reviews/{trade_id}/rerun", headers={"X-Clav-Token": "s3cret"})
    assert with_token.status_code == 200


def test_rerun_then_later_pass_appends_second_review_without_deleting_first(
    app_and_factory,
) -> None:
    app, factory = app_and_factory
    trade_id = _seed_closed_trade(factory, symbol="AAPL")
    _insert_review(factory, trade_id, calibration="overconfident", tags=["first"])

    rerun_resp = TestClient(app).post(f"/api/reviews/{trade_id}/rerun")
    assert rerun_resp.status_code == 200

    class _FakeAnalyst(Analyst):
        def analyze(self, symbol, news, social_digest, context):
            raise NotImplementedError

        def review(self, trade, context) -> TradeReview:
            return TradeReview(
                why_entered="second look",
                confidence_calibration="calibrated",
                tags=["second"],
                model="fake",
            )

    # A later pass, strictly after the first review's created_at, so
    # "newest first" ordering is unambiguous.
    later_clock = FakeClock(NOW.replace(hour=13))
    service = TradeReviewService(analyst=_FakeAnalyst(), session_factory=factory, clock=later_clock)
    service.run_pass()

    session = factory()
    repos = Repositories(session)
    history = repos.trade_reviews.list_for_trade(trade_id)  # newest first
    assert len(history) == 2
    assert history[0].tags == ["second"]
    assert history[1].tags == ["first"]
    row = repos.trades.get(trade_id)
    assert row.review_status == "reviewed"
    session.close()


def test_ui_rerun_form_redirects_and_resets(app_and_factory) -> None:
    app, factory = app_and_factory
    trade_id = _seed_closed_trade(factory, symbol="AAPL")
    _insert_review(factory, trade_id)
    _mark_failed(factory, trade_id, attempts=5)

    resp = TestClient(app, follow_redirects=False).post(f"/reviews/{trade_id}/rerun")
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/reviews/{trade_id}"

    session = factory()
    repos = Repositories(session)
    row = repos.trades.get(trade_id)
    assert row.review_status == "pending"
    assert row.review_attempts == 0
    session.close()


def test_detail_page_shows_rerun_button_for_reviewed_or_failed_not_pending(
    app_and_factory,
) -> None:
    app, factory = app_and_factory
    pending_id = _seed_closed_trade(factory, symbol="AAPL", tag="p")
    reviewed_id = _seed_closed_trade(factory, symbol="MSFT", tag="r")
    _insert_review(factory, reviewed_id)

    pending_resp = TestClient(app).get(f"/reviews/{pending_id}")
    assert "Force a re-review" not in pending_resp.text

    reviewed_resp = TestClient(app).get(f"/reviews/{reviewed_id}")
    assert "Force a re-review" in reviewed_resp.text
