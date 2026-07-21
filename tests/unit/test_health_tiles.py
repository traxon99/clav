"""Story 4.8 — at-a-glance dashboard health tiles: liveness, breaker/budget
badges, freshness-by-source, system resources, and the daily-loss gauge, all
built by ``build_health_view`` from the Story-4.1 health_snapshot. Every
tile degrades to "unknown" rather than crashing when a metric is missing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from clav.clock import FakeClock
from clav.config import Settings
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.web.health_view import build_health_view
from clav.web.main import create_app

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _tiles_by_name(view: dict) -> dict[str, dict]:
    return {t["name"]: t for t in view["tiles"]}


# --- build_health_view (pure, no HTTP) --------------------------------------


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _seed_snapshot(repos: Repositories, categories: dict, ts: datetime = NOW) -> None:
    snapshot = {"ts": ts.isoformat(), "categories": categories}
    repos.system_control.set(
        "health_snapshot", json.dumps(snapshot), updated_at=ts, updated_by="system:test"
    )


def test_fresh_install_renders_unknown_everywhere(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        view = build_health_view(repos, NOW, scan_interval_minutes=30)

    assert view["status"] == "ok"
    assert view["has_snapshot"] is False
    tiles = _tiles_by_name(view)
    assert tiles["liveness"]["display"] == "unknown"
    assert tiles["alpaca"]["status"] == "unknown"
    assert tiles["gemini"]["status"] == "unknown"
    assert tiles["freshness"]["status"] == "unknown"
    assert tiles["system"]["status"] == "unknown"
    assert tiles["daily P&L vs cap"]["status"] == "unknown"


def test_populated_snapshot_renders_each_tile(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.scan_cycles.create("c1", started_at=NOW, mode="dryrun", trigger="scheduled")
        repos.scan_cycles.finish("c1", finished_at=NOW - timedelta(minutes=2), status="completed")
        _seed_snapshot(
            repos,
            {
                "external": {
                    "alpaca": {"status": "ok", "value": {"ok": True}},
                    "gemini": {
                        "status": "ok",
                        "value": {"daily_tokens": 100, "daily_token_budget": 1000},
                    },
                },
                "freshness": {
                    "AAPL:candle": {"status": "ok", "value": {"age_hours": 1.0}},
                    "MSFT:candle": {"status": "warn", "value": {"age_hours": 8.0}},
                },
                "system": {
                    "free_memory": {"status": "ok", "value": {"free_mb": 512.0}},
                },
                "trading": {
                    "daily_pnl_vs_cap": {
                        "status": "ok",
                        "value": {"daily_pnl_pct": -0.01, "cap": 0.03},
                    }
                },
            },
        )
        view = build_health_view(repos, NOW, scan_interval_minutes=30)

    tiles = _tiles_by_name(view)
    assert tiles["liveness"]["display"] == "120s since last cycle"
    assert tiles["alpaca"]["status"] == "ok"
    assert tiles["gemini"]["display"] == "100/1000 tokens"
    # worst-of-kind: MSFT's warn beats AAPL's ok for the "candle" freshness tile.
    assert tiles["freshness: candle"]["status"] == "warn"
    assert tiles["free_memory"]["display"] == "free_mb=512.0"
    assert tiles["daily P&L vs cap"]["display"] == "-1.00% (cap 3.00%)"


def test_gemini_breaker_open_noted_in_display(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_snapshot(
            repos,
            {
                "external": {
                    "gemini": {
                        "status": "critical",
                        "value": {
                            "daily_tokens": 50,
                            "daily_token_budget": 1000,
                            "breaker_open": True,
                        },
                    }
                }
            },
        )
        view = build_health_view(repos, NOW, scan_interval_minutes=30)

    tile = _tiles_by_name(view)["gemini"]
    assert tile["status"] == "critical"
    assert "breaker open" in tile["display"]


def test_daily_pnl_gauge_reflects_critical_status(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_snapshot(
            repos,
            {
                "trading": {
                    "daily_pnl_vs_cap": {
                        "status": "critical",
                        "value": {"daily_pnl_pct": -0.05, "cap": 0.03},
                    }
                }
            },
        )
        view = build_health_view(repos, NOW, scan_interval_minutes=30)

    tile = _tiles_by_name(view)["daily P&L vs cap"]
    assert tile["status"] == "critical"
    assert tile["display"] == "-5.00% (cap 3.00%)"


def test_missing_specific_value_field_renders_unknown_not_crash(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        # daily_pnl_vs_cap present but without the expected value fields.
        _seed_snapshot(repos, {"trading": {"daily_pnl_vs_cap": {"status": "ok", "value": {}}}})
        view = build_health_view(repos, NOW, scan_interval_minutes=30)

    tile = _tiles_by_name(view)["daily P&L vs cap"]
    assert tile["status"] == "ok"
    assert tile["display"] == "unknown"


# --- HTTP-level smoke tests --------------------------------------------------


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


def test_dashboard_includes_health_tiles(app_and_factory) -> None:
    app, _ = app_and_factory
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="health-tiles"' in resp.text
    assert "liveness" in resp.text
    assert "status-unknown" in resp.text  # fresh install -- nothing collected yet


def test_health_tiles_partial_route_renders_standalone_fragment(app_and_factory) -> None:
    app, _ = app_and_factory
    client = TestClient(app)
    resp = client.get("/partials/health-tiles")
    assert resp.status_code == 200
    assert 'id="health-tiles"' in resp.text
    # a fragment, not a full page -- no nav/doctype, so HTMX can swap it cleanly.
    assert "<html" not in resp.text
    assert "<nav>" not in resp.text


def test_health_tiles_partial_polls_via_htmx(app_and_factory) -> None:
    app, _ = app_and_factory
    client = TestClient(app)
    resp = client.get("/")
    assert 'hx-get="/partials/health-tiles"' in resp.text
    assert "hx-trigger=" in resp.text


def test_dashboard_reflects_critical_health_event_as_a_tile(app_and_factory) -> None:
    app, factory = app_and_factory
    session = factory()
    repos = Repositories(session)
    repos.system_control.set(
        "health_snapshot",
        json.dumps(
            {
                "ts": NOW.isoformat(),
                "categories": {"external": {"alpaca": {"status": "critical", "value": {}}}},
            }
        ),
        updated_at=NOW,
        updated_by="system:test",
    )
    session.commit()
    session.close()

    client = TestClient(app)
    resp = client.get("/")
    assert "status-critical" in resp.text
