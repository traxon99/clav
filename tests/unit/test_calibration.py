"""Story 4.9 — descriptive calibration view: bucket/hit-rate math over closed
trades joined to the decision that drove them. Explicitly descriptive (epic
decision #6) -- no scored calibration model. Must handle small/empty samples
without dividing by zero."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from clav.data.db import make_engine, make_session_factory
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import OrderRequest, TradeDecision
from clav.web.calibration import build_calibration_view

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _seed_closed_trade(
    factory,
    *,
    symbol: str = "AAPL",
    conviction: float | None,
    realized_pl: float,
    return_pct: float,
    cycle_id: str,
) -> None:
    """A closed trade with (or without) a Gemini-driven decision behind it."""
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create(symbol)
    repos.scan_cycles.create(cycle_id, started_at=NOW, mode="dryrun", trigger="scheduled")

    reasoning = {} if conviction is None else {"llm": {"conviction": conviction, "sentiment": 0.5}}
    decision = TradeDecision(
        cycle_id=cycle_id,
        symbol=symbol,
        action="BUY",
        target_qty=10,
        raw_score=0.5,
        technical_score=0.3,
        llm_signal=0.0 if conviction is None else conviction,
        portfolio_bias=0.0,
        reasoning=reasoning,
    )
    decision_id = repos.decisions.add(
        scan_cycle_id=cycle_id, instrument_id=instrument.id, decision=decision, created_at=NOW
    )
    order_row = repos.orders.create(
        instrument_id=instrument.id,
        decision_id=decision_id,
        request=OrderRequest(
            client_order_id=f"clav-{cycle_id}-{symbol}-buy", symbol=symbol, side="buy", qty=10
        ),
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
        exit_price=100.0 + realized_pl / 10,
        closed_at=NOW,
        realized_pl=realized_pl,
        return_pct=return_pct,
    )
    session.commit()
    session.close()


def test_empty_sample_renders_without_dividing_by_zero(factory) -> None:
    session = factory()
    repos = Repositories(session)
    view = build_calibration_view(repos)
    session.close()

    assert view["sample_count"] == 0
    assert view["gemini_mean_return_pct"] is None
    assert view["gemini_hit_rate"] is None
    for bucket in view["buckets"]:
        assert bucket.count == 0
        assert bucket.mean_return_pct is None
        assert bucket.hit_rate is None


def test_buckets_trades_by_conviction_magnitude(factory) -> None:
    _seed_closed_trade(factory, conviction=0.1, realized_pl=10.0, return_pct=0.05, cycle_id="c1")
    _seed_closed_trade(factory, conviction=0.9, realized_pl=50.0, return_pct=0.2, cycle_id="c2")
    _seed_closed_trade(factory, conviction=-0.85, realized_pl=-20.0, return_pct=-0.1, cycle_id="c3")

    session = factory()
    repos = Repositories(session)
    view = build_calibration_view(repos)
    session.close()

    by_label = {b.label: b for b in view["buckets"]}
    assert by_label["0.0-0.25"].count == 1
    assert by_label["0.75-1.0"].count == 2  # 0.9 and |-0.85|
    assert by_label["0.75-1.0"].hit_rate == pytest.approx(0.5)  # one win, one loss


def test_hit_rate_and_mean_return_computed_correctly(factory) -> None:
    _seed_closed_trade(factory, conviction=0.8, realized_pl=100.0, return_pct=0.1, cycle_id="c1")
    _seed_closed_trade(factory, conviction=0.85, realized_pl=-50.0, return_pct=-0.05, cycle_id="c2")

    session = factory()
    repos = Repositories(session)
    view = build_calibration_view(repos)
    session.close()

    assert view["gemini_count"] == 2
    assert view["gemini_hit_rate"] == pytest.approx(0.5)
    assert view["gemini_mean_return_pct"] == pytest.approx((0.1 - 0.05) / 2)


def test_technical_only_trades_tracked_separately_from_gemini(factory) -> None:
    _seed_closed_trade(factory, conviction=None, realized_pl=30.0, return_pct=0.03, cycle_id="c1")
    _seed_closed_trade(factory, conviction=0.6, realized_pl=10.0, return_pct=0.01, cycle_id="c2")

    session = factory()
    repos = Repositories(session)
    view = build_calibration_view(repos)
    session.close()

    assert view["technical_count"] == 1
    assert view["gemini_count"] == 1
    assert view["technical_mean_return_pct"] == pytest.approx(0.03)
    assert view["sample_count"] == 2


def test_single_trade_sample_does_not_crash(factory) -> None:
    _seed_closed_trade(factory, conviction=0.5, realized_pl=5.0, return_pct=0.01, cycle_id="c1")

    session = factory()
    repos = Repositories(session)
    view = build_calibration_view(repos)
    session.close()

    assert view["gemini_count"] == 1
    assert view["gemini_hit_rate"] == 1.0
