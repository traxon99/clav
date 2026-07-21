"""Story 4.9 — GET /calibration: renders the descriptive conviction-vs-
outcome view (scatter + bucketed summary tables) end to end through the
FastAPI route, including the empty-DB state."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from clav.clock import FakeClock
from clav.config import Settings
from clav.data.db import make_engine
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import OrderRequest, TradeDecision
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
    clock = FakeClock(NOW)
    app = create_app(cfg, clock=clock)
    return app, app.state.session_factory


def test_empty_history_renders_gracefully(app_and_factory) -> None:
    app, _ = app_and_factory
    resp = TestClient(app).get("/calibration")
    assert resp.status_code == 200
    assert "not enough data yet" in resp.text
    assert "0 closed trades" in resp.text


def test_renders_seeded_gemini_and_technical_trades(app_and_factory) -> None:
    app, factory = app_and_factory
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create("AAPL")
    repos.scan_cycles.create("c1", started_at=NOW, mode="dryrun", trigger="scheduled")

    decision = TradeDecision(
        cycle_id="c1",
        symbol="AAPL",
        action="BUY",
        target_qty=10,
        raw_score=0.5,
        technical_score=0.3,
        llm_signal=0.8,
        portfolio_bias=0.0,
        reasoning={"llm": {"conviction": 0.8, "sentiment": 0.6}},
    )
    decision_id = repos.decisions.add(
        scan_cycle_id="c1", instrument_id=instrument.id, decision=decision, created_at=NOW
    )
    order_row = repos.orders.create(
        instrument_id=instrument.id,
        decision_id=decision_id,
        request=OrderRequest(client_order_id="clav-c1-AAPL-buy", symbol="AAPL", side="buy", qty=10),
        submitted_at=NOW,
    )
    trade_row = repos.trades.open_trade(
        instrument_id=instrument.id,
        entry_order_id=order_row.id,
        entry_decision_id=decision_id,
        qty=10,
        entry_price=100.0,
        opened_at=NOW,
    )
    repos.trades.close_trade(
        trade_row.id,
        exit_order_id=order_row.id,
        exit_price=115.0,
        closed_at=NOW,
        realized_pl=150.0,
        return_pct=0.15,
    )
    session.commit()
    session.close()

    resp = TestClient(app).get("/calibration")
    assert resp.status_code == 200
    body = resp.text
    assert "1 closed trade" in body
    assert "1 Gemini-driven" in body
    assert "0 technical-only" in body
    assert "<circle" in body
    assert "15.00%" in body
    assert "100%" in body  # hit rate
