"""Discover page + on-demand analysis routes (autonomous-discovery epic):
render, enqueue a request, ticker autocomplete, tradability validation, token."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from clav.clock import FakeClock
from clav.config import Settings
from clav.data.db import make_engine
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.services.discovery import DISCOVERY_SNAPSHOT_KEY
from clav.web.main import create_app

NOW = datetime(2026, 7, 23, 15, 0, tzinfo=UTC)


def _settings(tmp_path, **extra) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        watchlist=["AAPL", "MSFT"],
        alpaca={"api_key": "k", "api_secret": "s"},
        data_dir=tmp_path,
        **extra,
    )


@pytest.fixture
def app_and_factory(tmp_path):
    cfg = _settings(tmp_path)
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))
    app = create_app(cfg, clock=FakeClock(NOW))
    return app, app.state.session_factory


def _repos(factory) -> Repositories:
    return Repositories(factory())


def test_discover_page_renders_pins_and_search(app_and_factory) -> None:
    app, _ = app_and_factory
    resp = TestClient(app).get("/discover")
    assert resp.status_code == 200
    assert "Discover" in resp.text
    assert "Analyze a ticker now" in resp.text
    assert "AAPL" in resp.text  # a pin


def test_watchlist_alias_still_serves_discover(app_and_factory) -> None:
    app, _ = app_and_factory
    resp = TestClient(app).get("/watchlist")
    assert resp.status_code == 200
    assert "Your pins" in resp.text


def test_analyze_enqueues_request(app_and_factory) -> None:
    app, factory = app_and_factory
    resp = TestClient(app).post("/analyze", data={"symbol": "nvda"}, follow_redirects=False)
    assert resp.status_code == 303
    session = factory()
    pending = Repositories(session).analysis_requests.list_pending(limit=5)
    assert [r.symbol for r in pending] == ["NVDA"]


def test_analyze_dedupes_pending(app_and_factory) -> None:
    app, factory = app_and_factory
    c = TestClient(app)
    c.post("/analyze", data={"symbol": "NVDA"})
    c.post("/analyze", data={"symbol": "nvda"})
    pending = Repositories(factory()).analysis_requests.list_pending(limit=5)
    assert len(pending) == 1


def test_analyze_rejects_untradable_when_catalog_present(app_and_factory) -> None:
    app, factory = app_and_factory
    session = factory()
    Repositories(session).assets.upsert_many(
        [{"symbol": "AAPL", "tradable": True}], updated_at=NOW
    )
    session.commit()
    resp = TestClient(app).post("/analyze", data={"symbol": "ZZZZ"})
    assert resp.status_code == 422
    assert "tradable symbol" in resp.text  # apostrophe is HTML-escaped by Jinja


def test_ticker_autocomplete_uses_catalog(app_and_factory) -> None:
    app, factory = app_and_factory
    session = factory()
    Repositories(session).assets.upsert_many(
        [
            {"symbol": "AAPL", "name": "Apple Inc", "tradable": True},
            {"symbol": "AMD", "name": "Advanced Micro", "tradable": True},
        ],
        updated_at=NOW,
    )
    session.commit()
    rows = TestClient(app).get("/api/tickers?q=A").json()
    symbols = {r["symbol"] for r in rows}
    assert "AAPL" in symbols and "AMD" in symbols


def test_ticker_autocomplete_falls_back_when_empty_catalog(app_and_factory) -> None:
    app, _ = app_and_factory
    rows = TestClient(app).get("/api/tickers?q=AAP").json()
    assert any(r["symbol"] == "AAPL" for r in rows)  # curated fallback


def test_discovered_board_renders_from_snapshot(app_and_factory) -> None:
    app, factory = app_and_factory
    import json

    session = factory()
    Repositories(session).system_control.set(
        DISCOVERY_SNAPSHOT_KEY,
        json.dumps(
            {"generated_at": NOW.isoformat(),
             "candidates": [{"symbol": "GME", "score": 0.9, "mention_volume": 500,
                             "anomaly_flag": True, "source": "stocktwits_trending"}]}
        ),
        updated_at=NOW,
        updated_by="discovery",
    )
    session.commit()
    resp = TestClient(app).get("/discover")
    assert "GME" in resp.text
    assert "unusual spike" in resp.text  # anomaly_flag surfaced


def test_discovery_state_live_interlock() -> None:
    # mode=live can't be built through Settings until Epic 6, so exercise the
    # router's gate helper directly with a stand-in cfg.
    from types import SimpleNamespace

    from clav.web.routers.ui import _discovery_state

    def cfg(mode, enabled, allow_live):
        return SimpleNamespace(
            mode=mode,
            sources=SimpleNamespace(
                discovery=SimpleNamespace(enabled=enabled, allow_live=allow_live)
            ),
        )

    # paper: enabled runs, not blocked
    assert _discovery_state(cfg("paper", True, False), None) == (True, False)
    # live + no opt-in: suppressed and flagged blocked
    assert _discovery_state(cfg("live", True, False), None) == (False, True)
    # live + explicit opt-in: runs
    assert _discovery_state(cfg("live", True, True), None) == (True, False)
    # live but discovery disabled: off, not "blocked" (nothing to block)
    assert _discovery_state(cfg("live", False, False), None) == (False, False)
    # a runtime override can enable it (still subject to the live interlock)
    ov = SimpleNamespace(discovery_enabled=True)
    assert _discovery_state(cfg("paper", False, False), ov) == (True, False)
    assert _discovery_state(cfg("live", False, False), ov) == (False, True)


def test_analyze_requires_token_when_configured(tmp_path) -> None:
    cfg = _settings(tmp_path, web={"token": "s3cret"})
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))
    app = create_app(cfg, clock=FakeClock(NOW))
    c = TestClient(app)
    assert c.post("/analyze", data={"symbol": "NVDA"}).status_code == 401
    ok = c.post("/analyze", data={"symbol": "NVDA", "_token": "s3cret"}, follow_redirects=False)
    assert ok.status_code == 303
