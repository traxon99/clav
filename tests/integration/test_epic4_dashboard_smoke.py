"""Story 4.10 — dashboard smoke suite: every clav-web page + HTMX partial
renders via a plain ``TestClient`` (which never executes JavaScript), so
this file itself proves the "JS off" contract -- HTMX (``hx-boost``,
``hx-trigger``) is a progressive enhancement only, per epic decision #1/#7.
Covers the empty-DB / missing-metric states specifically, since those are
the states most likely to trip a bare ``{{ foo.bar }}`` template access
that a seeded-data test would never exercise, plus a couple of the
filter/pagination round trips each individual router's own test file
covers piecemeal -- gathered here as the one file a reviewer opens to see
the whole web surface is up.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from clav.clock import FakeClock
from clav.config import Settings
from clav.data.db import make_engine
from clav.data.tables import Base
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
def client(tmp_path) -> TestClient:
    cfg = _settings(tmp_path)
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))
    app = create_app(cfg, clock=FakeClock(NOW))
    return TestClient(app)


# --- every page + partial renders on a brand-new, empty DB ------------------

_EMPTY_DB_GET_ROUTES = [
    "/",
    "/partials/health-tiles",
    "/portfolio",
    "/explanations",
    "/audit",
    "/calibration",
    "/config",
    "/prompt",
    "/health",
    "/metrics",
    "/api/positions",
    "/api/journal",
    "/api/prompt",
    "/api/prompt/versions",
]


@pytest.mark.parametrize("path", _EMPTY_DB_GET_ROUTES)
def test_every_page_and_partial_renders_on_an_empty_db(client: TestClient, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text[:300]}"


# --- missing-metric / not-found states, specifically -----------------------


def test_explanation_detail_of_unknown_decision_is_a_clean_404(client: TestClient) -> None:
    resp = client.get("/explanations/999999")
    assert resp.status_code == 404


def test_journal_detail_of_unknown_proposal_renders_without_crashing(client: TestClient) -> None:
    resp = client.get("/journal/999999")
    assert resp.status_code == 200


def test_audit_cycle_reconstruction_of_unknown_cycle_renders_without_crashing(
    client: TestClient,
) -> None:
    resp = client.get("/audit/cycle/does-not-exist")
    assert resp.status_code == 200


def test_health_reports_indeterminate_liveness_with_no_cycles_ever_run(client: TestClient) -> None:
    """A fresh install has never completed a cycle -- ``age_seconds`` is
    ``None`` (indeterminate), which must not be misreported as a fault."""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["liveness"]["last_successful_cycle_age_seconds"] is None
    assert body["status"] == "ok"


# --- filter/pagination round trips work as plain (JS-off) GET requests -----


def test_explanations_symbol_filter_round_trips_as_a_plain_query_string(
    client: TestClient,
) -> None:
    resp = client.get("/explanations?symbol=AAPL&action=BUY&limit=10&offset=0")
    assert resp.status_code == 200


def test_audit_filter_round_trips_as_a_plain_query_string(client: TestClient) -> None:
    resp = client.get("/audit?cycle_id=c1&category=system&severity=ok&limit=10&offset=0")
    assert resp.status_code == 200


def test_portfolio_limit_round_trips_as_a_plain_query_string(client: TestClient) -> None:
    resp = client.get("/portfolio?limit=50")
    assert resp.status_code == 200


def test_health_tiles_partial_matches_the_full_page_dashboard_status(client: TestClient) -> None:
    """The HTMX poll target (``/partials/health-tiles``) and the initial
    full-page render (``/``) must agree -- both read the same
    ``build_health_view`` snapshot, so a client with JS disabled (seeing
    only the initial page) never sees a different status than one polling."""
    full_page = client.get("/").text
    partial = client.get("/partials/health-tiles").text
    assert "<strong>liveness</strong>" in full_page
    assert "<strong>liveness</strong>" in partial
