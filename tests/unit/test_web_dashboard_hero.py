"""GET / (dashboard): the Robinhood-style portfolio-value hero -- big
current-equity number, signed change vs. a selectable period (1H/1D/1W/YTD/
1Y via ?period=), and an equity sparkline -- sitting above the existing
health tiles / controls / recent-decisions journal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from clav.clock import FakeClock
from clav.config import Settings
from clav.data.db import make_engine
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import Candle, PortfolioSnapshot, Position
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
def app_and_factory(tmp_path):
    cfg = _settings(tmp_path)
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))
    app = create_app(cfg, clock=FakeClock(NOW))
    return app, app.state.session_factory


def test_hero_shows_placeholder_with_no_snapshots(app_and_factory) -> None:
    app, _ = app_and_factory
    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert "No portfolio snapshot yet." in resp.text
    assert "not enough data yet" in resp.text
    # all five period tabs are always offered, even with no data.
    for label in ["1H", "1D", "1W", "YTD", "1Y"]:
        assert f">{label}<" in resp.text


def test_hero_shows_current_equity_and_gain(app_and_factory) -> None:
    app, factory = app_and_factory
    session = factory()
    repos = Repositories(session)
    repos.portfolio_snapshots.add(
        PortfolioSnapshot(
            ts=NOW - timedelta(days=10), cash=9000.0, equity=9500.0, buying_power=9000.0
        )
    )
    repos.portfolio_snapshots.add(
        PortfolioSnapshot(ts=NOW, cash=9000.0, equity=10_000.0, buying_power=9000.0)
    )
    session.commit()
    session.close()

    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "$10000.00" in body or "$10,000.00" in body
    assert "hero-change up" in body
    assert "+$500.00" in body


def test_period_query_param_selects_the_active_tab(app_and_factory) -> None:
    app, _ = app_and_factory
    resp = TestClient(app).get("/?period=1w")
    assert resp.status_code == 200
    assert 'class="period-tab active"' in resp.text
    assert 'href="/?period=1w" class="period-tab active"' in resp.text


def test_invalid_period_falls_back_to_default_without_crashing(app_and_factory) -> None:
    app, _ = app_and_factory
    resp = TestClient(app).get("/?period=nonsense")
    assert resp.status_code == 200
    assert 'href="/?period=1d" class="period-tab active"' in resp.text


def test_positions_fieldset_links_to_portfolio_details(app_and_factory) -> None:
    app, _ = app_and_factory
    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert 'href="/portfolio"' in resp.text


def test_open_positions_table_shows_current_price_cost_avg_and_pct(app_and_factory) -> None:
    app, factory = app_and_factory
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create("AAPL")
    repos.candles.upsert_many(
        instrument.id,
        [
            Candle(
                symbol="AAPL",
                timeframe="1Day",
                open=195,
                high=196,
                low=189,
                close=195.0,
                volume=1000,
                ts=NOW,
            )
        ],
    )
    repos.positions.upsert(
        instrument.id, Position(symbol="AAPL", qty=10, avg_entry_price=180.0), opened_at=NOW
    )
    session.commit()
    session.close()

    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "Cost avg" in body
    assert "Current price" in body
    assert "180.00" in body  # cost average
    assert "195.00" in body  # current (last-close) price
    assert "150.00" in body  # unrealized P/L
    assert "8.33%" in body  # unrealized P/L %
