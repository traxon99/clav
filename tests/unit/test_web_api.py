"""Story 3.8 — control API: optional token enforcement, journal list/detail +
approve flow (DB-only, no broker), config round-trip with validation,
positions/health payload shape."""

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
def app_and_repos(tmp_path):
    cfg = _settings(tmp_path)
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))
    clock = FakeClock(NOW)
    app = create_app(cfg, clock=clock)
    return app, app.state.session_factory


def _seed_proposal(session_factory, *, status="pending"):
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
        llm_signal=0.0,
        portfolio_bias=0.0,
        reasoning={"llm": {"rationale": "strong catalyst"}},
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
        status=status,
        created_at=NOW,
    )
    session.commit()
    proposal_id = proposal.id
    session.close()
    return proposal_id


def test_health_ok(app_and_repos) -> None:
    app, _ = app_and_repos
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mode"] == "paper"
    assert body["emergency_stop"] is False
    assert body["paused"] is False


def test_health_reports_live_mode(tmp_path) -> None:
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

    resp = client.get("/health")

    assert resp.json()["mode"] == "live"


def test_journal_list_and_detail(app_and_repos) -> None:
    app, factory = app_and_repos
    proposal_id = _seed_proposal(factory)
    client = TestClient(app)

    listed = client.get("/api/journal").json()
    assert len(listed) == 1
    assert listed[0]["id"] == proposal_id
    assert listed[0]["status"] == "pending"

    detail = client.get(f"/api/journal/{proposal_id}").json()
    assert detail["proposal"]["rationale"] == "strong catalyst"
    assert detail["decision"]["action"] == "BUY"
    assert detail["risk_evaluation"]["approved"] is True


def test_journal_detail_404(app_and_repos) -> None:
    app, _ = app_and_repos
    client = TestClient(app)
    resp = client.get("/api/journal/999")
    assert resp.status_code == 404


def test_approve_is_db_only_and_marks_approved_not_executed(app_and_repos) -> None:
    """The web process must never touch the broker: approving a pending
    proposal here only flips it to "approved" -- clav-core executes it on its
    next cycle (see services/decision_journal.py execute_pending_approvals)."""
    app, factory = app_and_repos
    proposal_id = _seed_proposal(factory)
    client = TestClient(app)

    resp = client.post(f"/api/journal/{proposal_id}/approve", params={"actor": "alice"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["decided_by"] == "alice"


def test_reject_marks_rejected(app_and_repos) -> None:
    app, factory = app_and_repos
    proposal_id = _seed_proposal(factory)
    client = TestClient(app)

    resp = client.post(f"/api/journal/{proposal_id}/reject", params={"actor": "alice"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


def test_approve_already_decided_is_a_noop(app_and_repos) -> None:
    app, factory = app_and_repos
    proposal_id = _seed_proposal(factory, status="rejected")
    client = TestClient(app)

    resp = client.post(f"/api/journal/{proposal_id}/approve", params={"actor": "alice"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"  # unchanged, fail-closed


def test_positions_shape_with_no_snapshot(app_and_repos) -> None:
    app, _ = app_and_repos
    client = TestClient(app)
    resp = client.get("/api/positions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["snapshot"] is None
    assert body["positions"] == []


def test_config_round_trip_with_validation(app_and_repos) -> None:
    app, _ = app_and_repos
    client = TestClient(app)

    empty = client.get("/api/config").json()
    assert empty["weights"] is None

    payload = {
        "weights": {"technical": 0.5, "llm": 0.3, "portfolio": 0.2},
        "watchlist": ["AAPL", "MSFT"],
    }
    put_resp = client.put("/api/config", json=payload)
    assert put_resp.status_code == 200
    assert put_resp.json()["weights"]["llm"] == 0.3

    got = client.get("/api/config").json()
    assert got["watchlist"] == ["AAPL", "MSFT"]


def test_config_put_rejects_weights_not_summing_to_one(app_and_repos) -> None:
    app, _ = app_and_repos
    client = TestClient(app)
    payload = {"weights": {"technical": 0.9, "llm": 0.5, "portfolio": 0.1}}
    resp = client.put("/api/config", json=payload)
    assert resp.status_code == 422


def test_prompt_get_default_and_edit(app_and_repos) -> None:
    app, _ = app_and_repos
    client = TestClient(app)

    initial = client.get("/api/prompt").json()
    assert initial["content"]  # seeded default persona
    assert initial["version_id"] is not None

    edit_resp = client.put("/api/prompt", json={"content": "new persona text"})
    assert edit_resp.status_code == 200
    assert edit_resp.json()["content"] == "new persona text"

    after = client.get("/api/prompt").json()
    assert after["content"] == "new persona text"

    versions = client.get("/api/prompt/versions").json()
    assert len(versions) == 2


def test_prompt_put_rejects_empty_content(app_and_repos) -> None:
    app, _ = app_and_repos
    client = TestClient(app)
    resp = client.put("/api/prompt", json={"content": "   "})
    assert resp.status_code == 422


def test_control_status_and_estop_flow(app_and_repos) -> None:
    app, _ = app_and_repos
    client = TestClient(app)

    status = client.get("/api/control").json()
    assert status == {"emergency_stop": False, "paused": False}

    trip = client.post("/api/control/estop", params={"actor": "alice"})
    assert trip.json()["emergency_stop"] is True

    clear = client.post("/api/control/estop/clear", params={"actor": "alice"})
    assert clear.json()["emergency_stop"] is False


def test_optional_token_enforced_only_on_writes(tmp_path) -> None:
    cfg = _settings(tmp_path, token="s3cret")
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))
    clock = FakeClock(NOW)
    app = create_app(cfg, clock=clock)
    client = TestClient(app)

    # Reads never require the token.
    assert client.get("/health").status_code == 200
    assert client.get("/api/control").status_code == 200

    # Writes without the token are rejected.
    no_token = client.post("/api/control/pause")
    assert no_token.status_code == 401

    # Writes with the correct token succeed.
    with_token = client.post("/api/control/pause", headers={"X-Clav-Token": "s3cret"})
    assert with_token.status_code == 200
    assert with_token.json()["paused"] is True


def test_token_off_by_default(app_and_repos) -> None:
    """No web.token configured -> writes need no header at all (epic decision
    #7: single-operator LAN network needs no app password)."""
    app, _ = app_and_repos
    client = TestClient(app)
    resp = client.post("/api/control/pause")
    assert resp.status_code == 200
