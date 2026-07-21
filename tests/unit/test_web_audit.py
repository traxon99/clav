"""Story 4.7 — Searchable audit/journal browser: GET /audit (filterable,
paginated, merging health_event + audit_log rows) and GET /audit/cycle/{id}
(reconstructs one cycle: config_snapshot -> decisions -> risk evals -> orders
-> health events -> audit log)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from clav.clock import FakeClock
from clav.config import Settings
from clav.data.db import make_engine
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import HealthEvent, OrderRequest, RiskDecision, TradeDecision
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


def _seed_health_event(
    factory, *, cycle_id: str = "c1", category: str = "freshness", status: str = "warn", ts=NOW
) -> None:
    session = factory()
    repos = Repositories(session)
    repos.scan_cycles.create(cycle_id, started_at=NOW, mode="dryrun", trigger="scheduled")
    repos.health_events.add_many(
        [
            HealthEvent(
                ts=ts,
                category=category,  # type: ignore[arg-type]
                name="candles_stale",
                status=status,  # type: ignore[arg-type]
                value={},
                cycle_id=cycle_id,
            )
        ]
    )
    session.commit()
    session.close()


def _seed_audit_entry(factory, *, correlation_id: str = "c1", ts=NOW) -> None:
    session = factory()
    repos = Repositories(session)
    repos.audit_log.add(
        ts=ts,
        actor="operator",
        action="pause",
        entity_type="control",
        entity_id="trading",
        correlation_id=correlation_id,
    )
    session.commit()
    session.close()


def _seed_full_cycle(factory, cycle_id: str = "c1") -> int:
    """A cycle with a config_snapshot, one decision + risk eval + order, a
    health event, and an audit log entry -- everything /audit/cycle/{id}
    resolves."""
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create("AAPL")
    repos.scan_cycles.create(cycle_id, started_at=NOW, mode="dryrun", trigger="scheduled")
    repos.config_snapshots.add_for_cycle(
        cycle_id, git_sha="abc123", config={"watchlist": ["AAPL"]}, created_at=NOW
    )
    decision = TradeDecision(
        cycle_id=cycle_id,
        symbol="AAPL",
        action="BUY",
        target_qty=10,
        raw_score=0.5,
        technical_score=0.3,
        llm_signal=0.0,
        portfolio_bias=0.0,
        reasoning={},
    )
    decision_id = repos.decisions.add(
        scan_cycle_id=cycle_id, instrument_id=instrument.id, decision=decision, created_at=NOW
    )
    repos.risk_evaluations.add(
        decision_id, RiskDecision(approved=True, adjusted_qty=10), evaluated_at=NOW
    )
    repos.orders.create(
        instrument_id=instrument.id,
        decision_id=decision_id,
        request=OrderRequest(
            client_order_id=f"clav-{cycle_id}-AAPL-buy", symbol="AAPL", side="buy", qty=10
        ),
        submitted_at=NOW,
    )
    repos.health_events.add_many(
        [
            HealthEvent(
                ts=NOW,
                category="system",
                name="memory_ok",
                status="ok",
                value={},
                cycle_id=cycle_id,
            )
        ]
    )
    repos.audit_log.add(
        ts=NOW,
        actor="operator",
        action="pause",
        entity_type="control",
        entity_id="trading",
        correlation_id=cycle_id,
    )
    session.commit()
    session.close()
    return decision_id


# --- list view ---------------------------------------------------------


def test_list_merges_health_and_audit_rows(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_health_event(factory, cycle_id="c1")
    _seed_audit_entry(factory, correlation_id="c1")

    resp = TestClient(app).get("/audit")
    assert resp.status_code == 200
    assert "candles_stale" in resp.text
    assert "operator pause on control/trading" in resp.text


def test_list_filters_by_cycle_id(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_health_event(factory, cycle_id="c1")
    _seed_health_event(factory, cycle_id="c2")

    resp = TestClient(app).get("/audit?cycle_id=c2")
    body = resp.text
    assert body.count("candles_stale") == 1


def test_list_filters_by_health_category(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_health_event(factory, cycle_id="c1", category="freshness")
    _seed_health_event(factory, cycle_id="c2", category="system")
    _seed_audit_entry(factory, correlation_id="c1")

    resp = TestClient(app).get("/audit?category=system")
    body = resp.text
    assert "system: candles_stale" in body
    assert "freshness: candles_stale" not in body
    assert "operator pause" not in body


def test_list_filters_by_severity_info_shows_only_audit(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_health_event(factory, cycle_id="c1", status="warn")
    _seed_audit_entry(factory, correlation_id="c1")

    resp = TestClient(app).get("/audit?severity=info")
    body = resp.text
    assert "operator pause" in body
    assert "candles_stale" not in body


def test_list_pagination_bounded_by_max_limit(app_and_factory) -> None:
    app, factory = app_and_factory
    for i in range(5):
        _seed_health_event(factory, cycle_id=f"c{i}", ts=NOW - timedelta(minutes=i))

    resp = TestClient(app).get("/audit?limit=999999")
    assert resp.status_code == 200
    resp2 = TestClient(app).get("/audit?limit=2")
    assert "Older" in resp2.text


def test_list_empty_renders_gracefully(app_and_factory) -> None:
    app, _ = app_and_factory
    resp = TestClient(app).get("/audit")
    assert resp.status_code == 200
    assert "No matching rows." in resp.text


# --- reconstruct-cycle view ---------------------------------------------


def test_reconstruct_cycle_end_to_end(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_full_cycle(factory, cycle_id="c1")

    resp = TestClient(app).get("/audit/cycle/c1")
    assert resp.status_code == 200
    body = resp.text
    assert "abc123" in body  # git sha
    assert "watchlist" in body  # config json
    assert "AAPL" in body
    assert "Approved" in body
    assert "buy" in body
    assert "memory_ok" in body
    assert "operator" in body


def test_reconstruct_cycle_unknown_id_renders_not_found(app_and_factory) -> None:
    app, _ = app_and_factory
    resp = TestClient(app).get("/audit/cycle/does-not-exist")
    assert resp.status_code == 200
    assert "Not found." in resp.text
