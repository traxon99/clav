from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import UniqueConstraint

from clav.data import tables
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import HealthEvent, OrderRequest
from clav.services.soak_report import build_soak_report, format_soak_report

NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
WINDOW_START = NOW - timedelta(hours=24)


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _create_schema_without_order_unique_constraint(engine) -> None:
    """Test-only: create the schema with ``order``'s UNIQUE(client_order_id)
    constraint temporarily removed, so a genuinely duplicated client_order_id
    can be seeded to exercise the soak report's detection query. The app's
    own write path (``OrderRepository.create``) can never produce this for
    real -- the DB constraint already guarantees that (see
    test_safety_invariants.py::test_invariant_3_client_order_id_is_globally_unique).
    This proves the report's *own* detection query is correct as a second,
    independent line of defense. ``Base.metadata`` is a shared, module-level
    object, so the constraint is restored immediately after DDL is emitted --
    this only affects the schema of ``engine``, never other tests."""
    order_table = tables.Order.__table__
    unique = next(c for c in order_table.constraints if isinstance(c, UniqueConstraint))
    order_table.constraints.discard(unique)
    try:
        Base.metadata.create_all(engine)
    finally:
        order_table.constraints.add(unique)


def test_empty_window_renders_cleanly(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        report = build_soak_report(
            repos,
            start=WINDOW_START,
            end=NOW,
            now=NOW,
            scan_interval_minutes=30,
            max_daily_loss_pct=0.03,
        )

    assert report.duplicate_order_count == 0
    assert report.unhandled_error_count == 0
    assert report.cycles_run == 0
    assert report.daily_pnl_pct is None
    assert report.daily_loss_headroom_pct is None
    # liveness is indeterminate (no cycle yet), which overall_status treats as ok
    assert report.liveness_status == "ok"
    assert report.clean is True

    text = format_soak_report(report)
    assert "CLEAN" in text
    assert "duplicate client_order_ids: 0" in text
    assert "daily P&L: n/a" in text


def test_clean_window_with_distinct_orders_has_no_duplicates(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        for i in range(3):
            req = OrderRequest(
                client_order_id=f"clav-c{i}-AAPL-buy", symbol="AAPL", side="buy", qty=1
            )
            repos.orders.create(
                instrument_id=instrument.id, decision_id=None, request=req, submitted_at=NOW
            )

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        report = build_soak_report(
            repos,
            start=WINDOW_START,
            end=NOW,
            now=NOW,
            scan_interval_minutes=30,
            max_daily_loss_pct=0.03,
        )

    assert report.duplicate_client_order_ids == {}
    assert report.duplicate_order_count == 0
    assert report.clean is True


def test_seeded_duplicate_client_order_id_is_flagged(tmp_path) -> None:
    engine = make_engine(tmp_path / "clav.db")
    _create_schema_without_order_unique_constraint(engine)
    factory = make_session_factory(engine)

    with session_scope(factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        req = OrderRequest(client_order_id="dup-order", symbol="AAPL", side="buy", qty=1)
        repos.orders.create(
            instrument_id=instrument.id, decision_id=None, request=req, submitted_at=NOW
        )
        repos.orders.create(
            instrument_id=instrument.id, decision_id=None, request=req, submitted_at=NOW
        )

    with session_scope(factory) as session:
        repos = Repositories(session)
        report = build_soak_report(
            repos,
            start=WINDOW_START,
            end=NOW,
            now=NOW,
            scan_interval_minutes=30,
            max_daily_loss_pct=0.03,
        )

    assert report.duplicate_client_order_ids == {"dup-order": 2}
    assert report.duplicate_order_count == 2
    assert report.clean is False
    assert "NOT CLEAN" in format_soak_report(report)


def test_failed_orders_and_stuck_cycles_are_counted(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        req = OrderRequest(client_order_id="clav-c1-AAPL-buy", symbol="AAPL", side="buy", qty=1)
        order = repos.orders.create(
            instrument_id=instrument.id, decision_id=None, request=req, submitted_at=NOW
        )
        order.status = "failed"

        # A cycle that started well over one scan interval ago and never
        # finished -- a crashed process, not merely still in-flight.
        repos.scan_cycles.create(
            "stuck-cycle", started_at=NOW - timedelta(hours=2), mode="paper", trigger="scheduled"
        )
        # A cycle that finished normally shouldn't count.
        repos.scan_cycles.create(
            "ok-cycle", started_at=NOW - timedelta(minutes=10), mode="paper", trigger="scheduled"
        )
        repos.scan_cycles.finish(
            "ok-cycle", finished_at=NOW - timedelta(minutes=9), status="completed"
        )

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        report = build_soak_report(
            repos,
            start=WINDOW_START,
            end=NOW,
            now=NOW,
            scan_interval_minutes=30,
            max_daily_loss_pct=0.03,
        )

    assert report.failed_orders == 1
    assert report.stuck_cycles == 1
    assert report.cycles_run == 2
    assert report.unhandled_error_count == 2
    assert report.clean is False


def test_daily_loss_headroom_from_latest_health_event(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.health_events.add_many(
            [
                HealthEvent(
                    ts=NOW,
                    category="trading",
                    name="daily_pnl_vs_cap",
                    status="ok",
                    value={"daily_pnl_pct": -0.01, "cap": 0.03},
                    cycle_id=None,
                )
            ]
        )

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        report = build_soak_report(
            repos,
            start=WINDOW_START,
            end=NOW,
            now=NOW,
            scan_interval_minutes=30,
            max_daily_loss_pct=0.03,
        )

    assert report.daily_pnl_pct == pytest.approx(-0.01)
    assert report.daily_loss_headroom_pct == pytest.approx(0.02)
    text = format_soak_report(report)
    assert "headroom 2.00%" in text


def test_critical_health_events_counted_in_window(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.health_events.add_many(
            [
                HealthEvent(
                    ts=NOW,
                    category="external",
                    name="alpaca",
                    status="critical",
                    value={},
                    cycle_id=None,
                ),
                HealthEvent(
                    ts=NOW, category="system", name="ram", status="ok", value={}, cycle_id=None
                ),
            ]
        )

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        report = build_soak_report(
            repos,
            start=WINDOW_START,
            end=NOW,
            now=NOW,
            scan_interval_minutes=30,
            max_daily_loss_pct=0.03,
        )

    assert report.critical_health_events == 1
