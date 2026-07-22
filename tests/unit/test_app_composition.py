"""Story 3.12 — the real clav-core composition root builds a fully-wired
ScanCycleService (news/social sources, GeminiAnalyst, decision journal,
runtime config) from a fresh config with **no paid keys configured** — the
epic-level DoD ("a fresh clone with no paid keys can run the full loop").

Story 5.4 — build_core_services() additionally returns a TradeReviewService
sharing the SAME GeminiAnalyst/GeminiBudget as the ScanCycleService's
AnalystGateway (epic-05 decision #3)."""

from __future__ import annotations

from clav.app import (
    _build_analyst,
    build_alerter,
    build_analyst_gateway,
    build_core_services,
)
from clav.clock import FakeClock
from clav.config import Settings
from clav.data.db import make_engine, make_session_factory
from clav.data.tables import Base
from clav.services.analyst_gateway import AnalystGateway
from clav.services.review import TradeReviewService
from clav.services.scan_cycle import ScanCycleService


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        watchlist=["AAPL"],
        alpaca={"api_key": "k", "api_secret": "s"},
        data_dir=tmp_path,
    )


def test_build_core_services_wires_analyst_gateway_with_no_paid_keys(tmp_path) -> None:
    cfg = _settings(tmp_path)
    assert cfg.llm.api_key is None
    assert cfg.newsapi.api_key is None
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))

    service, review_service = build_core_services(cfg, clock=FakeClock())

    assert isinstance(service, ScanCycleService)
    assert isinstance(review_service, TradeReviewService)
    assert service._analyst_gateway is not None
    assert isinstance(service._analyst_gateway, AnalystGateway)
    # epic-05 decision #3: the SAME analyst (and therefore the same
    # GeminiBudget/breaker) backs both entry analysis and trade review.
    assert review_service._analyst is service._analyst_gateway._analyst
    assert service._approval_policy is not None
    assert service._approval_policy.mode == "auto"  # config default
    assert service._runtime_config is not None
    # Story 4.3: alert_hook and the HealthMonitor's alerter are both wired,
    # not left None like Epic 3 shipped them.
    assert service._alert_hook is not None
    assert service._health_monitor is not None
    assert service._health_monitor._alerter is not None
    # Story 4.4: the boot-config snapshot base + a resolved git SHA are wired
    # so config_snapshot rows are meaningful, not empty/placeholder, and
    # secrets stay redacted (Settings.to_snapshot_dict()'s existing job).
    assert service._config_snapshot_base["watchlist"] == ["AAPL"]
    assert service._config_snapshot_base["alpaca"]["api_key"] == "**********"
    assert isinstance(service._git_sha, str)
    assert service._git_sha != ""


def test_build_alerter_has_no_channels_when_both_disabled(tmp_path) -> None:
    cfg = _settings(tmp_path)
    assert cfg.alerts.smtp.enabled is False
    assert cfg.alerts.webhook.enabled is False

    alerter = build_alerter(cfg, clock=FakeClock())

    assert alerter._channels == []


def test_build_alerter_builds_configured_channels(tmp_path) -> None:
    cfg = Settings(
        _env_file=None,  # type: ignore[call-arg]
        watchlist=["AAPL"],
        alpaca={"api_key": "k", "api_secret": "s"},
        data_dir=tmp_path,
        alerts={
            "smtp": {
                "enabled": True,
                "host": "smtp.example.com",
                "from_addr": "clav@example.com",
                "to_addr": "you@example.com",
            },
            "webhook": {"enabled": True, "url": "https://ntfy.sh/topic"},
        },
    )

    alerter = build_alerter(cfg, clock=FakeClock())

    assert len(alerter._channels) == 2


def test_build_analyst_gateway_defaults_to_free_keyless_sources(tmp_path) -> None:
    cfg = _settings(tmp_path)
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    clock = FakeClock()
    analyst, budget, capture, _review_capture = _build_analyst(
        cfg, session_factory=session_factory, clock=clock
    )

    gateway = build_analyst_gateway(
        cfg, analyst=analyst, budget=budget, capture=capture, clock=clock
    )

    # RSS + EDGAR on by default (keyless); NewsAPI off (no key configured).
    assert len(gateway._news_sources) == 2
    # Reddit + StockTwits on by default (keyless public endpoints).
    assert len(gateway._social_sources) == 2
