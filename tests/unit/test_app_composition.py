"""Story 3.12 — the real clav-core composition root builds a fully-wired
ScanCycleService (news/social sources, GeminiAnalyst, decision journal,
runtime config) from a fresh config with **no paid keys configured** — the
epic-level DoD ("a fresh clone with no paid keys can run the full loop")."""

from __future__ import annotations

from clav.app import build_analyst_gateway, build_scan_cycle_service
from clav.clock import FakeClock
from clav.config import Settings
from clav.data.db import make_engine, make_session_factory
from clav.data.tables import Base
from clav.services.analyst_gateway import AnalystGateway
from clav.services.scan_cycle import ScanCycleService


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        watchlist=["AAPL"],
        alpaca={"api_key": "k", "api_secret": "s"},
        data_dir=tmp_path,
    )


def test_build_scan_cycle_service_wires_analyst_gateway_with_no_paid_keys(tmp_path) -> None:
    cfg = _settings(tmp_path)
    assert cfg.llm.api_key is None
    assert cfg.newsapi.api_key is None
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))

    service = build_scan_cycle_service(cfg, clock=FakeClock())

    assert isinstance(service, ScanCycleService)
    assert service._analyst_gateway is not None
    assert isinstance(service._analyst_gateway, AnalystGateway)
    assert service._approval_policy is not None
    assert service._approval_policy.mode == "auto"  # config default
    assert service._runtime_config is not None


def test_build_analyst_gateway_defaults_to_free_keyless_sources(tmp_path) -> None:
    cfg = _settings(tmp_path)
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)

    gateway = build_analyst_gateway(cfg, session_factory=session_factory, clock=FakeClock())

    # RSS + EDGAR on by default (keyless); NewsAPI off (no key configured).
    assert len(gateway._news_sources) == 2
    # Reddit + StockTwits on by default (keyless public endpoints).
    assert len(gateway._social_sources) == 2
