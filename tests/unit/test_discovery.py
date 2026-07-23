"""DiscoveryService: the sentiment funnel that shortlists NEW tickers for the
analyst. Pure orchestration over fail-open sources + repos, so tested against an
in-memory DB with fake sources."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from clav.clock import FakeClock
from clav.data.db import make_engine, make_session_factory
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import DiscoveryCandidate
from clav.interfaces.discovery import DiscoverySource
from clav.services.discovery import DISCOVERY_SNAPSHOT_KEY, DiscoveryService

NOW = datetime(2026, 7, 23, 15, 0, tzinfo=UTC)


class FakeSource(DiscoverySource):
    def __init__(self, candidates: list[DiscoveryCandidate], *, boom: bool = False) -> None:
        self._c = candidates
        self._boom = boom

    def fetch(self) -> list[DiscoveryCandidate]:
        if self._boom:
            raise RuntimeError("source down")
        return self._c


def _cand(symbol: str, score: float, vol: int = 0) -> DiscoveryCandidate:
    return DiscoveryCandidate(symbol=symbol, score=score, mention_volume=vol, source="fake")


@pytest.fixture
def repos(tmp_path):
    eng = make_engine(tmp_path / "d.db")
    Base.metadata.create_all(eng)
    session = make_session_factory(eng)()
    return Repositories(session)


def _svc(sources, **kw) -> DiscoveryService:
    return DiscoveryService(sources, clock=FakeClock(NOW), **kw)


def test_ranks_and_caps(repos) -> None:
    svc = _svc([FakeSource([_cand("A", 0.2), _cand("B", 0.9), _cand("C", 0.5)])],
               max_candidates_per_cycle=2)
    out = svc.candidates_for_cycle(repos, pins=set(), open_symbols=set())
    assert out == ["B", "C"]  # ranked by score desc, capped at 2


def test_merges_same_symbol_keeping_max_score(repos) -> None:
    svc = _svc([FakeSource([_cand("A", 0.3)]), FakeSource([_cand("a", 0.8)])])
    out = svc.candidates_for_cycle(repos, pins=set(), open_symbols=set())
    assert out == ["A"]


def test_excludes_pins_and_open_positions(repos) -> None:
    svc = _svc([FakeSource([_cand("PIN", 0.9), _cand("HELD", 0.8), _cand("NEW", 0.7)])])
    out = svc.candidates_for_cycle(repos, pins={"PIN"}, open_symbols={"HELD"})
    assert out == ["NEW"]


def test_min_score_floor(repos) -> None:
    svc = _svc([FakeSource([_cand("A", 0.1), _cand("B", 0.6)])], min_score=0.5)
    assert svc.candidates_for_cycle(repos, pins=set(), open_symbols=set()) == ["B"]


def test_untradable_dropped_only_when_catalog_present(repos) -> None:
    svc = _svc([FakeSource([_cand("GOOD", 0.9), _cand("BADX", 0.8)])])
    # empty catalog -> no veto, both survive
    assert svc.candidates_for_cycle(repos, pins=set(), open_symbols=set()) == ["GOOD", "BADX"]
    # populate catalog with only GOOD tradable
    repos.assets.upsert_many(
        [{"symbol": "GOOD", "tradable": True}, {"symbol": "BADX", "tradable": False}],
        updated_at=NOW,
    )
    assert svc.candidates_for_cycle(repos, pins=set(), open_symbols=set()) == ["GOOD"]


def test_fail_open_source_yields_no_candidates(repos) -> None:
    svc = _svc([FakeSource([], boom=True), FakeSource([_cand("OK", 0.5)])])
    assert svc.candidates_for_cycle(repos, pins=set(), open_symbols=set()) == ["OK"]


def test_persists_snapshot_for_ui(repos) -> None:
    svc = _svc([FakeSource([_cand("A", 0.9, vol=1000)])])
    svc.candidates_for_cycle(repos, pins=set(), open_symbols=set())
    raw = repos.system_control.get(DISCOVERY_SNAPSHOT_KEY)
    assert raw is not None
    snap = json.loads(raw)
    assert snap["candidates"][0]["symbol"] == "A"
    assert snap["candidates"][0]["mention_volume"] == 1000
