"""Story 4.10 — RAM/bound discipline guard tests: every dashboard-reachable
query must be paginated/bounded regardless of what a caller passes as
``?limit=``, so a large table (thousands of snapshots/decisions/proposals)
never gets loaded whole into a single request on the Pi. Each test seeds
well past a route's configured cap and asserts the response still reads at
most that cap, not "however many rows exist"."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from clav.clock import FakeClock
from clav.config import Settings
from clav.data.db import make_engine
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import HealthEvent, PortfolioSnapshot, TradeDecision
from clav.web.main import create_app
from clav.web.routers.audit import MAX_PAGE_LIMIT as AUDIT_MAX_PAGE_LIMIT
from clav.web.routers.explanations import MAX_PAGE_LIMIT as EXPLANATIONS_MAX_PAGE_LIMIT
from clav.web.routers.journal import MAX_JOURNAL_LIMIT as API_JOURNAL_MAX_LIMIT
from clav.web.routers.portfolio import MAX_SNAPSHOT_LIMIT
from clav.web.routers.prompt import MAX_VERSIONS_LIMIT
from clav.web.routers.ui import MAX_JOURNAL_LIMIT as UI_JOURNAL_MAX_LIMIT

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


def test_portfolio_equity_route_is_bounded(app_and_factory) -> None:
    app, factory = app_and_factory
    session = factory()
    repos = Repositories(session)
    for i in range(MAX_SNAPSHOT_LIMIT + 250):
        repos.portfolio_snapshots.add(
            PortfolioSnapshot(
                ts=NOW - timedelta(minutes=(MAX_SNAPSHOT_LIMIT + 250 - i)),
                cash=1000.0,
                equity=1000.0 + i,
                buying_power=1000.0,
            )
        )
    session.commit()
    session.close()

    resp = TestClient(app).get("/portfolio?limit=999999999")
    assert resp.status_code == 200
    assert f"last {MAX_SNAPSHOT_LIMIT} snapshots" in resp.text


def test_explanations_list_is_bounded(app_and_factory) -> None:
    app, factory = app_and_factory
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create("AAPL")
    count = EXPLANATIONS_MAX_PAGE_LIMIT + 50
    for i in range(count):
        cycle_id = f"c{i}"
        repos.scan_cycles.create(cycle_id, started_at=NOW, mode="dryrun", trigger="scheduled")
        decision = TradeDecision(
            cycle_id=cycle_id,
            symbol="AAPL",
            action="BUY",
            target_qty=1,
            raw_score=0.1,
            technical_score=0.1,
            llm_signal=0.0,
            portfolio_bias=0.0,
            reasoning={},
        )
        repos.decisions.add(
            scan_cycle_id=cycle_id, instrument_id=instrument.id, decision=decision, created_at=NOW
        )
    session.commit()
    session.close()

    resp = TestClient(app).get("/explanations?limit=999999999")
    assert resp.status_code == 200
    # every returned row is one decision's action cell; the page can never
    # render more of them than the configured cap.
    assert resp.text.count('class="action-BUY"') <= EXPLANATIONS_MAX_PAGE_LIMIT


def test_audit_list_is_bounded(app_and_factory) -> None:
    app, factory = app_and_factory
    session = factory()
    repos = Repositories(session)
    count = AUDIT_MAX_PAGE_LIMIT + 50
    for i in range(count):
        cycle_id = f"c{i}"
        repos.scan_cycles.create(cycle_id, started_at=NOW, mode="dryrun", trigger="scheduled")
        repos.health_events.add_many(
            [
                HealthEvent(
                    ts=NOW - timedelta(minutes=count - i),
                    category="system",
                    name="memory_ok",
                    status="ok",
                    value={},
                    cycle_id=cycle_id,
                )
            ]
        )
    session.commit()
    session.close()

    resp = TestClient(app).get("/audit?limit=999999999")
    assert resp.status_code == 200
    assert resp.text.count("memory_ok") <= AUDIT_MAX_PAGE_LIMIT


def _seed_decision(repos: Repositories, instrument_id: int, cycle_id: str) -> int:
    repos.scan_cycles.create(cycle_id, started_at=NOW, mode="dryrun", trigger="scheduled")
    decision = TradeDecision(
        cycle_id=cycle_id,
        symbol="AAPL",
        action="BUY",
        target_qty=1,
        raw_score=0.1,
        technical_score=0.1,
        llm_signal=0.0,
        portfolio_bias=0.0,
        reasoning={},
    )
    return repos.decisions.add(
        scan_cycle_id=cycle_id, instrument_id=instrument_id, decision=decision, created_at=NOW
    )


def test_dashboard_journal_is_bounded(app_and_factory) -> None:
    app, factory = app_and_factory
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create("AAPL")
    decision_id = _seed_decision(repos, instrument.id, "c0")
    count = UI_JOURNAL_MAX_LIMIT + 50
    for i in range(count):
        repos.trade_proposals.create(
            decision_id=decision_id,
            symbol="AAPL",
            side="buy",
            proposed_qty=1,
            rationale="r",
            inputs_ref={},
            status="executed",
            created_at=NOW - timedelta(minutes=count - i),
        )
    session.commit()
    session.close()

    resp = TestClient(app).get("/?limit=999999999")
    assert resp.status_code == 200
    assert resp.text.count("<tr>") - 1 <= UI_JOURNAL_MAX_LIMIT  # -1 for the header row


def test_api_journal_is_bounded(app_and_factory) -> None:
    app, factory = app_and_factory
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create("AAPL")
    decision_id = _seed_decision(repos, instrument.id, "c0")
    count = API_JOURNAL_MAX_LIMIT + 50
    for i in range(count):
        repos.trade_proposals.create(
            decision_id=decision_id,
            symbol="AAPL",
            side="buy",
            proposed_qty=1,
            rationale="r",
            inputs_ref={},
            status="executed",
            created_at=NOW - timedelta(minutes=count - i),
        )
    session.commit()
    session.close()

    resp = TestClient(app).get("/api/journal?limit=999999999")
    assert resp.status_code == 200
    assert len(resp.json()) == API_JOURNAL_MAX_LIMIT


def test_api_prompt_versions_is_bounded(app_and_factory) -> None:
    app, _ = app_and_factory
    store = app.state.prompt_store
    for i in range(MAX_VERSIONS_LIMIT + 50):
        store.edit(f"persona revision {i}", created_by="operator")

    resp = TestClient(app).get("/api/prompt/versions?limit=999999999")
    assert resp.status_code == 200
    assert len(resp.json()) <= MAX_VERSIONS_LIMIT
