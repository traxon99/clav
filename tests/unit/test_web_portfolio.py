"""Story 4.5 — GET /portfolio: equity/drawdown charts (inline SVG), current
exposure/sector allocation, and the open-positions table with a
mark-to-last-close unrealized P&L. Read-only; the snapshot query is bounded
so the page never loads full history into RAM."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from clav.clock import FakeClock
from clav.config import Settings
from clav.data.db import make_engine, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import Candle, PortfolioSnapshot, Position
from clav.web.main import create_app
from clav.web.routers.portfolio import MAX_SNAPSHOT_LIMIT

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


def test_empty_history_renders_gracefully(app_and_factory) -> None:
    app, _ = app_and_factory
    client = TestClient(app)
    resp = client.get("/portfolio")
    assert resp.status_code == 200
    assert "No portfolio snapshot yet." in resp.text
    assert "No open positions." in resp.text
    assert "not enough data yet" in resp.text


def test_renders_seeded_snapshots_positions_and_sectors(app_and_factory) -> None:
    app, factory = app_and_factory
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create("AAPL", sector="Technology")

    for i in range(5):
        repos.portfolio_snapshots.add(
            PortfolioSnapshot(
                ts=NOW - timedelta(days=5 - i),
                cash=9000.0 + i * 10,
                equity=10_000.0 + i * 100,
                buying_power=9000.0,
                gross_exposure=1000.0,
                net_exposure=1000.0,
                drawdown=0.01 * i,
                peak_equity=10_500.0,
                sector_allocation={"Technology": 0.25},
            )
        )
    repos.candles.upsert_many(
        instrument.id,
        [
            Candle(
                symbol="AAPL",
                timeframe="1Day",
                open=190,
                high=196,
                low=189,
                close=195.0,
                volume=1000,
                ts=NOW,
            )
        ],
    )
    repos.positions.upsert(
        instrument.id,
        Position(
            symbol="AAPL", qty=10, avg_entry_price=180.0, stop_price=170.0, take_profit_price=210.0
        ),
        opened_at=NOW,
    )
    session.commit()
    session.close()

    client = TestClient(app)
    resp = client.get("/portfolio")
    assert resp.status_code == 200
    assert "10400.00" in resp.text or "10,400.00" in resp.text or "10400.0" in resp.text
    assert "AAPL" in resp.text
    assert "Technology" in resp.text
    # unrealized P/L = (195 - 180) * 10 = 150.00
    assert "150.00" in resp.text
    # unrealized P/L % = 150 / (10 * 180) = 8.33%
    assert "8.33%" in resp.text
    assert "<polyline" in resp.text


def test_position_without_candle_data_shows_dash(app_and_factory) -> None:
    app, factory = app_and_factory
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create("AAPL")
    repos.positions.upsert(
        instrument.id, Position(symbol="AAPL", qty=5, avg_entry_price=100.0), opened_at=NOW
    )
    session.commit()
    session.close()

    client = TestClient(app)
    resp = client.get("/portfolio")
    assert resp.status_code == 200
    assert "AAPL" in resp.text
    assert "—" in resp.text  # unrealized P/L and last close both unknown


def test_snapshot_query_is_bounded(app_and_factory) -> None:
    app, factory = app_and_factory
    session = factory()
    repos = Repositories(session)
    for i in range(MAX_SNAPSHOT_LIMIT + 50):
        repos.portfolio_snapshots.add(
            PortfolioSnapshot(
                ts=NOW - timedelta(minutes=(MAX_SNAPSHOT_LIMIT + 50 - i)),
                cash=1000.0,
                equity=1000.0 + i,
                buying_power=1000.0,
            )
        )
    session.commit()
    session.close()

    with session_scope(factory) as session:
        repos = Repositories(session)
        # a request for "everything" is still clamped at the repo layer.
        rows = repos.portfolio_snapshots.get_recent(limit=10_000)
        assert len(rows) == MAX_SNAPSHOT_LIMIT + 50  # repo itself just honors the limit given

    client = TestClient(app)
    resp = client.get("/portfolio?limit=999999")
    assert resp.status_code == 200
    assert f"last {MAX_SNAPSHOT_LIMIT} snapshots" in resp.text
