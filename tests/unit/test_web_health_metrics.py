"""Story 4.2 — rich GET /health (status derivation over the Story-4.1
health_snapshot + last_cycle) and GET /metrics (Prometheus text exposition).
Both are strictly read-only over rows HealthMonitor already wrote."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from clav.clock import FakeClock
from clav.config import Settings
from clav.data.db import make_engine
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.web.main import create_app

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _settings(tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        watchlist=["AAPL"],
        alpaca={"api_key": "k", "api_secret": "s"},
        data_dir=tmp_path,
        scan_interval_minutes=30,
    )


@pytest.fixture
def app_and_factory(tmp_path):
    cfg = _settings(tmp_path)
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))
    clock = FakeClock(NOW)
    app = create_app(cfg, clock=clock)
    return app, app.state.session_factory


def _seed_completed_cycle(session_factory, *, finished_at: datetime, cycle_id: str = "c1") -> None:
    session = session_factory()
    repos = Repositories(session)
    repos.scan_cycles.create(cycle_id, started_at=finished_at, mode="dryrun", trigger="scheduled")
    repos.scan_cycles.finish(cycle_id, finished_at=finished_at, status="completed")
    session.commit()
    session.close()


def _seed_health_snapshot(session_factory, *, categories: dict, ts: datetime = NOW) -> None:
    session = session_factory()
    repos = Repositories(session)
    snapshot = {"ts": ts.isoformat(), "categories": categories}
    repos.system_control.set(
        "health_snapshot", json.dumps(snapshot), updated_at=ts, updated_by="system:test"
    )
    session.commit()
    session.close()


SAMPLE_CATEGORIES = {
    "external": {
        "alpaca": {"status": "ok", "value": {"ok": True}},
        "gemini": {"status": "warn", "value": {"budget_exhausted": True, "daily_tokens": 500}},
    },
    "system": {
        "free_memory": {"status": "ok", "value": {"free_mb": 512.0}},
    },
}


def test_health_ok_on_fresh_install(app_and_factory) -> None:
    app, _ = app_and_factory
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["emergency_stop"] is False
    assert body["last_cycle"] is None
    assert body["categories"] is None
    assert body["liveness"]["last_successful_cycle_age_seconds"] is None


def test_health_reflects_health_snapshot_categories(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_completed_cycle(factory, finished_at=NOW - timedelta(minutes=5))
    _seed_health_snapshot(factory, categories=SAMPLE_CATEGORIES)
    client = TestClient(app)

    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["categories"]["external"]["alpaca"]["status"] == "ok"
    assert body["categories"]["external"]["gemini"]["status"] == "warn"
    assert body["liveness"]["last_successful_cycle_age_seconds"] == pytest.approx(300.0)
    assert body["snapshot_ts"] == NOW.isoformat()


def test_health_degraded_when_critical_health_event_present(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_completed_cycle(factory, finished_at=NOW - timedelta(minutes=1))
    critical_categories = {
        "external": {"alpaca": {"status": "critical", "value": {"ok": False}}},
    }
    _seed_health_snapshot(factory, categories=critical_categories)
    client = TestClient(app)

    body = client.get("/health").json()
    assert body["status"] == "degraded"


def test_health_degraded_past_two_missed_cycles(app_and_factory) -> None:
    app, factory = app_and_factory
    client = TestClient(app)

    # scan_interval_minutes=30 -> degraded past 60min, down past 120min.
    _seed_completed_cycle(factory, finished_at=NOW - timedelta(minutes=90), cycle_id="c1")
    assert client.get("/health").json()["status"] == "degraded"


def test_health_down_when_last_cycle_very_stale(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_completed_cycle(factory, finished_at=NOW - timedelta(hours=3), cycle_id="c1")
    client = TestClient(app)
    assert client.get("/health").json()["status"] == "down"


def test_health_ignores_a_still_running_cycle(app_and_factory) -> None:
    """A cycle that hasn't finished yet shouldn't be misread as a stale
    completion — status stays 'ok' rather than flagging 'down'."""
    app, factory = app_and_factory
    session = factory()
    repos = Repositories(session)
    repos.scan_cycles.create(
        "c1", started_at=NOW - timedelta(hours=5), mode="dryrun", trigger="scheduled"
    )
    session.commit()
    session.close()

    client = TestClient(app)
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["liveness"]["last_successful_cycle_age_seconds"] is None


def test_metrics_returns_prometheus_text_with_expected_series(app_and_factory) -> None:
    app, factory = app_and_factory
    _seed_completed_cycle(factory, finished_at=NOW - timedelta(minutes=10))
    _seed_health_snapshot(factory, categories=SAMPLE_CATEGORIES)
    client = TestClient(app)

    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    text = resp.text

    assert "# TYPE clav_last_cycle_age_seconds gauge" in text
    assert "clav_last_cycle_age_seconds 600.0" in text
    assert "clav_emergency_stop 0.0" in text
    assert "clav_paused 0.0" in text
    assert 'clav_health_status{category="external",name="alpaca"} 0.0' in text
    assert 'clav_health_status{category="external",name="gemini"} 1.0' in text
    assert 'clav_health_value{category="system",name="free_memory",metric="free_mb"} 512.0' in text


def test_metrics_is_valid_without_a_health_snapshot(app_and_factory) -> None:
    """Fresh install: no HealthMonitor run yet -- /metrics must still return
    parseable text, not error, and simply omit the health_* series."""
    app, _ = app_and_factory
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    assert "clav_emergency_stop 0.0" in text
    assert "clav_health_status" not in text
    # every non-comment, non-empty line is a valid "name{labels} value" sample
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name_and_labels, _, value = line.rpartition(" ")
        assert name_and_labels
        float(value)  # must parse as a number
