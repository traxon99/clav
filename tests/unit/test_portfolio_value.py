"""The dashboard's portfolio-value hero: current equity, signed change (abs +
%) against a selectable lookback period (1H/1D/1W/YTD/1Y), and a color-coded
equity sparkline for that period. Descriptive over ``portfolio_snapshot``
history only; must handle empty/thin history without dividing by zero or
crashing on a missing baseline."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from clav.data.db import make_engine, make_session_factory
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import PortfolioSnapshot
from clav.web.portfolio_value import build_portfolio_value_view

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _add_snapshot(repos: Repositories, *, ts: datetime, equity: float) -> None:
    repos.portfolio_snapshots.add(
        PortfolioSnapshot(ts=ts, cash=1000.0, equity=equity, buying_power=1000.0)
    )


def test_no_snapshots_renders_without_data(factory) -> None:
    session = factory()
    repos = Repositories(session)
    view = build_portfolio_value_view(repos, NOW, "1d")
    session.close()

    assert view["has_data"] is False
    assert view["period"] == "1d"
    assert "not enough data yet" in view["chart_svg"]


def test_unknown_period_falls_back_to_default(factory) -> None:
    session = factory()
    repos = Repositories(session)
    _add_snapshot(repos, ts=NOW, equity=1000.0)
    session.commit()

    view = build_portfolio_value_view(repos, NOW, "bogus")
    session.close()

    assert view["period"] == "1d"


def test_single_snapshot_has_no_change(factory) -> None:
    session = factory()
    repos = Repositories(session)
    _add_snapshot(repos, ts=NOW, equity=10_000.0)
    session.commit()

    view = build_portfolio_value_view(repos, NOW, "1w")
    session.close()

    assert view["has_data"] is True
    assert view["current_equity"] == 10_000.0
    assert view["change_abs"] == 0.0
    assert view["change_pct"] == 0.0
    assert view["is_gain"] is True


def test_change_measured_against_baseline_before_the_cutoff(factory) -> None:
    session = factory()
    repos = Repositories(session)
    _add_snapshot(repos, ts=NOW - timedelta(days=10), equity=9_000.0)  # before the 1w cutoff
    _add_snapshot(repos, ts=NOW - timedelta(days=3), equity=9_500.0)  # inside the window
    _add_snapshot(repos, ts=NOW, equity=10_000.0)
    session.commit()

    view = build_portfolio_value_view(repos, NOW, "1w")
    session.close()

    # baseline is the last snapshot at/before (now - 7d) -- the 9_000 one,
    # not the 9_500 one that's already inside the window.
    assert view["change_abs"] == pytest.approx(1_000.0)
    assert view["change_pct"] == pytest.approx(1_000.0 / 9_000.0)
    assert view["is_gain"] is True


def test_loss_is_flagged_and_colored_red(factory) -> None:
    session = factory()
    repos = Repositories(session)
    _add_snapshot(repos, ts=NOW - timedelta(days=10), equity=10_000.0)
    _add_snapshot(repos, ts=NOW - timedelta(days=3), equity=9_700.0)
    _add_snapshot(repos, ts=NOW, equity=9_500.0)
    session.commit()

    view = build_portfolio_value_view(repos, NOW, "1w")
    session.close()

    assert view["is_gain"] is False
    assert view["change_abs"] == pytest.approx(-500.0)
    assert "#b02a2a" in view["chart_svg"]  # loss color


def test_no_baseline_old_enough_falls_back_to_earliest_available(factory) -> None:
    """History doesn't reach back to the 1y cutoff yet -- the earliest point
    we do have becomes the baseline instead of crashing on a missing row."""
    session = factory()
    repos = Repositories(session)
    _add_snapshot(repos, ts=NOW - timedelta(days=2), equity=9_800.0)
    _add_snapshot(repos, ts=NOW, equity=10_000.0)
    session.commit()

    view = build_portfolio_value_view(repos, NOW, "1y")
    session.close()

    assert view["change_abs"] == pytest.approx(200.0)


def test_ytd_cutoff_is_january_first(factory) -> None:
    session = factory()
    repos = Repositories(session)
    _add_snapshot(repos, ts=datetime(2025, 12, 31, tzinfo=UTC), equity=9_000.0)
    _add_snapshot(repos, ts=datetime(2026, 1, 15, tzinfo=UTC), equity=9_500.0)
    _add_snapshot(repos, ts=NOW, equity=10_000.0)
    session.commit()

    view = build_portfolio_value_view(repos, NOW, "ytd")
    session.close()

    # baseline is the last snapshot at/before Jan 1 2026 -- the Dec 31 one.
    assert view["change_abs"] == pytest.approx(1_000.0)


def test_1h_period_cutoff(factory) -> None:
    session = factory()
    repos = Repositories(session)
    _add_snapshot(repos, ts=NOW - timedelta(hours=6), equity=9_800.0)
    _add_snapshot(repos, ts=NOW - timedelta(minutes=30), equity=9_900.0)
    _add_snapshot(repos, ts=NOW, equity=10_000.0)
    session.commit()

    view = build_portfolio_value_view(repos, NOW, "1h")
    session.close()

    # baseline is the last snapshot at/before (now - 1h) -- the 6h-old one.
    assert view["change_abs"] == pytest.approx(200.0)


def test_single_snapshot_still_draws_a_flat_line(factory) -> None:
    """A single snapshot (e.g. a brand-new $0 account, or a quiet day with no
    trades) must still render a spanning line, not the "not enough data"
    empty state -- flat at the one known equity value across the whole
    period, from its start out to now."""
    session = factory()
    repos = Repositories(session)
    _add_snapshot(repos, ts=NOW, equity=0.0)
    session.commit()

    view = build_portfolio_value_view(repos, NOW, "1y")
    session.close()

    assert "not enough data" not in view["chart_svg"]
    assert 'points="' in view["chart_svg"]


def test_flat_period_draws_two_points_at_the_same_value(factory) -> None:
    """Zoomed out to a period with no history that old, the line should be
    flat from the period start up to the point where real movement begins --
    not just a single dangling point."""
    session = factory()
    repos = Repositories(session)
    _add_snapshot(repos, ts=NOW - timedelta(hours=2), equity=10_000.0)
    session.commit()

    view = build_portfolio_value_view(repos, NOW, "1d")
    session.close()

    assert "not enough data" not in view["chart_svg"]
    assert "range 1e+04 to 1e+04" in view["chart_svg"]  # flat: lo == hi


def test_stale_latest_snapshot_still_draws_a_flat_line(factory) -> None:
    """The only snapshot in the DB is older than the period cutoff (capture
    has gone quiet for longer than the selected period) -- it becomes both
    the "baseline" and the "latest" row. Anchoring the line on those two
    timestamps directly would collapse to a single point since they're
    literally the same row; the line must still span cutoff -> now flat."""
    session = factory()
    repos = Repositories(session)
    _add_snapshot(repos, ts=NOW - timedelta(days=2), equity=99_645.96)
    session.commit()

    view = build_portfolio_value_view(repos, NOW, "1d")
    session.close()

    assert "not enough data" not in view["chart_svg"]
    assert "range 9.965e+04 to 9.965e+04" in view["chart_svg"]  # flat


def test_periods_list_marks_exactly_one_active(factory) -> None:
    session = factory()
    repos = Repositories(session)
    view = build_portfolio_value_view(repos, NOW, "1w")
    session.close()

    active = [p for p in view["periods"] if p["active"]]
    assert len(active) == 1
    assert active[0]["key"] == "1w"
