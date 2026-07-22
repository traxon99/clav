"""Story 3.9 — HTMX/server-rendered UI smoke tests: templates render, forms
round-trip, e-stop/pause/approve/reject/config/prompt all work via plain HTML
POSTs (no JS required)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from clav.clock import FakeClock
from clav.config import Settings
from clav.data.db import make_engine
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import RiskDecision, TradeDecision
from clav.web.main import create_app

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _settings(tmp_path, *, token: str | None = None) -> Settings:
    overrides: dict = {
        "watchlist": ["AAPL"],
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
    app = create_app(cfg, clock=FakeClock(NOW))
    return app, app.state.session_factory


def _seed_pending_proposal(session_factory) -> int:
    session = session_factory()
    repos = Repositories(session)
    inst = repos.instruments.get_or_create("AAPL")
    repos.scan_cycles.create("c1", started_at=NOW, mode="dryrun", trigger="manual")
    decision = TradeDecision(
        cycle_id="c1",
        symbol="AAPL",
        action="BUY",
        target_qty=10,
        raw_score=0.5,
        technical_score=0.5,
        llm_signal=0.4,
        portfolio_bias=0.0,
        reasoning={
            "llm": {
                "sentiment": 0.8,
                "conviction": 0.5,
                "prompt_version": "1",
                "model": "fake",
                "is_fallback": False,
                "rationale": "strong catalyst",
            }
        },
    )
    decision_id = repos.decisions.add(
        scan_cycle_id="c1", instrument_id=inst.id, decision=decision, created_at=NOW
    )
    risk = RiskDecision(approved=True, adjusted_qty=10, blocked_by=[], notes={})
    repos.risk_evaluations.add(decision_id, risk, evaluated_at=NOW)
    proposal = repos.trade_proposals.create(
        decision_id=decision_id,
        symbol="AAPL",
        side="buy",
        proposed_qty=10,
        rationale="strong catalyst",
        inputs_ref={"news_item_ids": [1]},
        status="pending",
        created_at=NOW,
    )
    session.commit()
    proposal_id = proposal.id
    session.close()
    return proposal_id


def test_dashboard_renders(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_pending_proposal(factory)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Decision Journal" in resp.text
    assert "AAPL" in resp.text
    assert "pending" in resp.text


def test_dashboard_renders_with_empty_journal(app_and_factory) -> None:
    app, _ = app_and_factory
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "No decisions yet" in resp.text


def test_paper_mode_never_renders_the_live_banner(app_and_factory) -> None:
    app, _ = app_and_factory
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "LIVE" not in resp.text


def test_live_mode_renders_the_live_banner_on_every_page(tmp_path) -> None:
    cfg = Settings(
        _env_file=None,  # type: ignore[call-arg]
        mode="live",
        i_understand_live_trading=True,
        watchlist=["AAPL"],
        alpaca={"api_key": "k", "api_secret": "s"},
        data_dir=tmp_path,
    )
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))
    app = create_app(cfg, clock=FakeClock(NOW))
    client = TestClient(app)

    # the banner reads request.app.state.cfg.mode via base.html — check it
    # shows up on more than one page, not just the one that happens to embed it.
    for path in ("/", "/portfolio", "/config"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert "LIVE" in resp.text
        assert "real-money trading is active" in resp.text


def test_journal_detail_renders_gemini_rationale(app_and_factory) -> None:
    app, factory = app_and_factory
    proposal_id = _seed_pending_proposal(factory)
    client = TestClient(app)
    resp = client.get(f"/journal/{proposal_id}")
    assert resp.status_code == 200
    assert "strong catalyst" in resp.text
    assert "Approve" in resp.text
    assert "Reject" in resp.text


def test_journal_detail_missing_shows_not_found(app_and_factory) -> None:
    app, _ = app_and_factory
    client = TestClient(app)
    resp = client.get("/journal/999")
    assert resp.status_code == 200
    assert "Not found" in resp.text


def test_approve_form_post_round_trips(app_and_factory) -> None:
    app, factory = app_and_factory
    proposal_id = _seed_pending_proposal(factory)
    client = TestClient(app)

    resp = client.post(
        f"/journal/{proposal_id}/approve", data={"actor": "alice"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/journal/{proposal_id}"

    detail = client.get(f"/journal/{proposal_id}")
    assert "approved" in detail.text
    # No longer offers the approve/reject action buttons once decided.
    assert ">Approve<" not in detail.text
    assert ">Reject<" not in detail.text


def test_reject_form_post_round_trips(app_and_factory) -> None:
    app, factory = app_and_factory
    proposal_id = _seed_pending_proposal(factory)
    client = TestClient(app)

    resp = client.post(
        f"/journal/{proposal_id}/reject", data={"actor": "alice"}, follow_redirects=False
    )
    assert resp.status_code == 303
    detail = client.get(f"/journal/{proposal_id}")
    assert "rejected" in detail.text


def test_control_pause_and_resume_round_trip_via_dashboard(app_and_factory) -> None:
    app, _ = app_and_factory
    client = TestClient(app)

    resp = client.post("/control/pause", data={"actor": "operator"}, follow_redirects=False)
    assert resp.status_code == 303
    dashboard = client.get("/")
    assert "paused: True" in dashboard.text

    client.post("/control/resume", data={"actor": "operator"})
    dashboard2 = client.get("/")
    assert "paused: False" in dashboard2.text


def test_prompt_page_renders_and_edit_round_trips(app_and_factory) -> None:
    app, _ = app_and_factory
    client = TestClient(app)

    page = client.get("/prompt")
    assert page.status_code == 200
    assert "Strategy prompt" in page.text

    resp = client.post(
        "/prompt",
        data={"content": "a brand new persona", "actor": "operator"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    after = client.get("/prompt")
    assert "a brand new persona" in after.text


def test_config_page_renders_and_edit_round_trips(app_and_factory) -> None:
    app, _ = app_and_factory
    client = TestClient(app)

    page = client.get("/config")
    assert page.status_code == 200
    assert "Weights, risk knobs" in page.text

    payload = {
        "technical": "0.5",
        "llm": "0.3",
        "portfolio": "0.2",
        "buy_threshold": "0.2",
        "sell_threshold": "-0.2",
        "max_position_value": "2000",
        "max_daily_loss_pct": "0.03",
        "max_drawdown_pct": "0.10",
        "max_portfolio_exposure_pct": "0.80",
        "max_sector_allocation_pct": "0.30",
        "cooldown_minutes": "60",
        "post_loss_cooldown_minutes": "120",
        "watchlist": "AAPL, MSFT",
        "actor": "operator",
    }
    resp = client.post("/config", data=payload, follow_redirects=False)
    assert resp.status_code == 303

    after = client.get("/config")
    assert 'value="0.3"' in after.text  # llm weight persisted
    assert "AAPL, MSFT" in after.text


def test_config_edit_rejects_weights_not_summing_to_one(app_and_factory) -> None:
    app, _ = app_and_factory
    client = TestClient(app)
    payload = {
        "technical": "0.9",
        "llm": "0.5",
        "portfolio": "0.1",
        "buy_threshold": "0.2",
        "sell_threshold": "-0.2",
        "max_position_value": "2000",
        "max_daily_loss_pct": "0.03",
        "max_drawdown_pct": "0.10",
        "max_portfolio_exposure_pct": "0.80",
        "max_sector_allocation_pct": "0.30",
        "cooldown_minutes": "60",
        "post_loss_cooldown_minutes": "120",
        "watchlist": "AAPL",
        "actor": "operator",
    }
    resp = client.post("/config", data=payload)
    assert resp.status_code == 422
    assert "sum to 1.0" in resp.text


def test_config_page_shows_analysis_effort_presets(app_and_factory) -> None:
    app, _ = app_and_factory
    client = TestClient(app)

    page = client.get("/config")
    assert page.status_code == 200
    assert "Fast" in page.text
    assert "Thoughtful" in page.text
    assert "gemini-3.1-flash-lite" in page.text  # Fast preset's tooltip


def test_applying_fast_preset_sets_llm_and_interval_override(app_and_factory) -> None:
    app, _ = app_and_factory
    client = TestClient(app)

    resp = client.post(
        "/config/preset", data={"preset": "fast", "actor": "operator"}, follow_redirects=False
    )
    assert resp.status_code == 303

    after = client.get("/config")
    assert "gemini-3.1-flash-lite" in after.text
    assert "thinking_budget=<code>0</code>" in after.text.replace(" ", "")
    assert "Fast (active)" in after.text


def test_unknown_preset_is_rejected(app_and_factory) -> None:
    app, _ = app_and_factory
    client = TestClient(app)

    resp = client.post("/config/preset", data={"preset": "bogus", "actor": "operator"})
    assert resp.status_code == 422


def test_saving_main_settings_form_preserves_an_applied_preset(app_and_factory) -> None:
    """Regression guard: before this fix, POSTing the weights/risk/watchlist
    form silently wiped any scan_interval_minutes/llm override that was set
    by something else (the API, or the preset buttons) -- RuntimeOverrides
    is a full replace, not a merge, and the form didn't carry those two
    fields forward."""
    app, _ = app_and_factory
    client = TestClient(app)

    client.post("/config/preset", data={"preset": "thoughtful", "actor": "operator"})

    payload = {
        "technical": "0.6",
        "llm": "0.4",
        "portfolio": "0.0",
        "buy_threshold": "0.2",
        "sell_threshold": "-0.2",
        "max_position_value": "2000",
        "max_daily_loss_pct": "0.03",
        "max_drawdown_pct": "0.10",
        "max_portfolio_exposure_pct": "0.80",
        "max_sector_allocation_pct": "0.30",
        "cooldown_minutes": "60",
        "post_loss_cooldown_minutes": "120",
        "watchlist": "AAPL",
        "actor": "operator",
    }
    resp = client.post("/config", data=payload, follow_redirects=False)
    assert resp.status_code == 303

    after = client.get("/config")
    assert "Thoughtful (active)" in after.text  # preset survived the unrelated save
    assert 'value="0.4"' in after.text  # and the weights save itself still took


def test_ui_write_requires_token_via_hidden_field_when_configured(tmp_path) -> None:
    cfg = _settings(tmp_path, token="s3cret")
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))
    app = create_app(cfg, clock=FakeClock(NOW))
    client = TestClient(app)

    # No token in the form -> rejected.
    no_token = client.post("/control/pause", data={"actor": "operator"})
    assert no_token.status_code == 401

    # Reads still need no token at all.
    assert client.get("/").status_code == 200

    # Token embedded as a hidden field (as the base template does) -> accepted.
    with_token = client.post(
        "/control/pause", data={"actor": "operator", "_token": "s3cret"}, follow_redirects=False
    )
    assert with_token.status_code == 303
