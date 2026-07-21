"""Story 4.4 — ConfigSnapshotRepository: one row per cycle, but consecutive
identical (git_sha, config) pairs collapse to a small pointer row instead of
duplicating the full JSON blob across thousands of unchanged cycles."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _seed_cycle(repos, cycle_id: str) -> None:
    repos.scan_cycles.create(cycle_id, started_at=NOW, mode="dryrun", trigger="scheduled")


def test_first_snapshot_stores_the_full_config(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        snap = repos.config_snapshots.add_for_cycle(
            "c1", git_sha="sha1", config={"watchlist": ["AAPL"]}, created_at=NOW
        )

    assert snap.cycle_id == "c1"
    assert snap.git_sha == "sha1"
    assert snap.config == {"watchlist": ["AAPL"]}


def test_consecutive_identical_config_collapses_to_a_pointer(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        _seed_cycle(repos, "c2")
        _seed_cycle(repos, "c3")

        repos.config_snapshots.add_for_cycle(
            "c1", git_sha="sha1", config={"watchlist": ["AAPL"]}, created_at=NOW
        )
        repos.config_snapshots.add_for_cycle(
            "c2", git_sha="sha1", config={"watchlist": ["AAPL"]}, created_at=NOW
        )
        repos.config_snapshots.add_for_cycle(
            "c3", git_sha="sha1", config={"watchlist": ["AAPL"]}, created_at=NOW
        )

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        # The domain view always resolves to the full config, regardless of
        # which cycle you ask about.
        assert repos.config_snapshots.get_by_cycle_id("c1").config == {"watchlist": ["AAPL"]}
        assert repos.config_snapshots.get_by_cycle_id("c2").config == {"watchlist": ["AAPL"]}
        assert repos.config_snapshots.get_by_cycle_id("c3").config == {"watchlist": ["AAPL"]}


def test_pointer_rows_do_not_duplicate_the_blob_on_disk(session_factory) -> None:
    from clav.data import tables

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        _seed_cycle(repos, "c2")
        repos.config_snapshots.add_for_cycle(
            "c1", git_sha="sha1", config={"big": "blob" * 100}, created_at=NOW
        )
        repos.config_snapshots.add_for_cycle(
            "c2", git_sha="sha1", config={"big": "blob" * 100}, created_at=NOW
        )

        row1 = session.query(tables.ConfigSnapshotRow).filter_by(cycle_id="c1").one()
        row2 = session.query(tables.ConfigSnapshotRow).filter_by(cycle_id="c2").one()

        assert row1.config is not None
        assert row2.config is None
        assert row2.same_as_snapshot_id == row1.id


def test_changed_config_creates_a_new_full_row(session_factory) -> None:
    from clav.data import tables

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        _seed_cycle(repos, "c2")
        repos.config_snapshots.add_for_cycle(
            "c1", git_sha="sha1", config={"watchlist": ["AAPL"]}, created_at=NOW
        )
        repos.config_snapshots.add_for_cycle(
            "c2", git_sha="sha1", config={"watchlist": ["AAPL", "MSFT"]}, created_at=NOW
        )

        row2 = session.query(tables.ConfigSnapshotRow).filter_by(cycle_id="c2").one()
        assert row2.config == {"watchlist": ["AAPL", "MSFT"]}
        assert row2.same_as_snapshot_id is None


def test_changed_git_sha_creates_a_new_full_row_even_with_same_config(session_factory) -> None:
    from clav.data import tables

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        _seed_cycle(repos, "c2")
        repos.config_snapshots.add_for_cycle(
            "c1", git_sha="sha1", config={"watchlist": ["AAPL"]}, created_at=NOW
        )
        repos.config_snapshots.add_for_cycle(
            "c2", git_sha="sha2", config={"watchlist": ["AAPL"]}, created_at=NOW
        )

        row2 = session.query(tables.ConfigSnapshotRow).filter_by(cycle_id="c2").one()
        assert row2.config == {"watchlist": ["AAPL"]}
        assert row2.same_as_snapshot_id is None


def test_pointer_chain_flattens_to_a_single_hop(session_factory) -> None:
    """A run of N identical cycles should all point at the *first* full row,
    not chain through each other (O(1) resolution regardless of streak
    length)."""
    from clav.data import tables

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        for i in range(4):
            _seed_cycle(repos, f"c{i}")
            repos.config_snapshots.add_for_cycle(
                f"c{i}", git_sha="sha1", config={"watchlist": ["AAPL"]}, created_at=NOW
            )

        first_row = session.query(tables.ConfigSnapshotRow).filter_by(cycle_id="c0").one()
        for i in range(1, 4):
            row = session.query(tables.ConfigSnapshotRow).filter_by(cycle_id=f"c{i}").one()
            assert row.same_as_snapshot_id == first_row.id


def test_get_by_cycle_id_returns_none_for_unknown_cycle(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        assert repos.config_snapshots.get_by_cycle_id("nonexistent") is None
