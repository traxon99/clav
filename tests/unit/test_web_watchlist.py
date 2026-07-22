"""Forward-facing watchlist UI: view the current watchlist, add a ticker with
autocomplete, and remove one — each editing only the runtime override's
``watchlist`` without disturbing weights/risk overrides, and never emptying it."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from clav.clock import FakeClock
from clav.config import (
    RuntimeOverrides,
    Settings,
    ThresholdsConfig,
    WeightsConfig,
)
from clav.data.db import make_engine
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.web.main import create_app

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


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
    app = create_app(cfg, clock=FakeClock(NOW))
    return app, app.state.session_factory


def _override(app, factory) -> RuntimeOverrides:
    session = factory()
    try:
        return app.state.runtime_config.get(Repositories(session))
    finally:
        session.close()


def test_watchlist_page_shows_boot_symbols(app_and_factory) -> None:
    app, _ = app_and_factory
    resp = TestClient(app).get("/watchlist")
    assert resp.status_code == 200
    assert "AAPL" in resp.text
    assert "MSFT" in resp.text
    # autocomplete datalist is present with curated suggestions
    assert "ticker-suggestions" in resp.text
    assert "Tesla" in resp.text  # a curated common ticker name


def test_add_ticker_creates_override_without_clobbering_weights(app_and_factory) -> None:
    app, factory = app_and_factory
    # Seed a weights override first.
    session = factory()
    app.state.runtime_config.set(
        Repositories(session),
        RuntimeOverrides(
            weights=WeightsConfig(technical=0.5, llm=0.3, portfolio=0.2),
            thresholds=ThresholdsConfig(buy=0.2, sell=-0.2),
            watchlist=["AAPL", "MSFT"],
        ),
        now=NOW,
        updated_by="operator",
    )
    session.commit()
    session.close()

    resp = TestClient(app).post(
        "/watchlist/add", data={"symbol": "nvda"}, follow_redirects=False
    )
    assert resp.status_code == 303

    override = _override(app, factory)
    assert override.watchlist == ["AAPL", "MSFT", "NVDA"]  # uppercased + appended
    # the weights override survived the watchlist edit
    assert override.weights is not None
    assert override.weights.technical == 0.5


def test_add_duplicate_is_noop(app_and_factory) -> None:
    app, factory = app_and_factory
    TestClient(app).post("/watchlist/add", data={"symbol": "AAPL"})
    override = _override(app, factory)
    # AAPL already effective (from boot) -> no duplicate, override still writes the set
    assert override.watchlist is None or override.watchlist.count("AAPL") == 1


def test_remove_ticker(app_and_factory) -> None:
    app, factory = app_and_factory
    resp = TestClient(app).post(
        "/watchlist/remove", data={"symbol": "MSFT"}, follow_redirects=False
    )
    assert resp.status_code == 303
    override = _override(app, factory)
    assert override.watchlist == ["AAPL"]


def test_remove_last_ticker_is_refused(app_and_factory) -> None:
    app, factory = app_and_factory
    TestClient(app).post("/watchlist/remove", data={"symbol": "MSFT"})
    # now only AAPL remains; removing it too must be refused (never empty)
    TestClient(app).post("/watchlist/remove", data={"symbol": "AAPL"})
    override = _override(app, factory)
    assert override.watchlist == ["AAPL"]


def test_add_requires_token_when_configured(tmp_path) -> None:
    cfg = Settings(  # type: ignore[call-arg]
        _env_file=None,
        watchlist=["AAPL"],
        alpaca={"api_key": "k", "api_secret": "s"},
        data_dir=tmp_path,
        web={"token": "s3cret"},
    )
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))
    app = create_app(cfg, clock=FakeClock(NOW))
    client = TestClient(app)

    assert client.post("/watchlist/add", data={"symbol": "NVDA"}).status_code == 401
    ok = client.post(
        "/watchlist/add", data={"symbol": "NVDA", "_token": "s3cret"}, follow_redirects=False
    )
    assert ok.status_code == 303
