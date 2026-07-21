"""Story 4.1 — HealthMonitor wired into a full scan cycle: health_event rows
land for every category, a health_snapshot is persisted, and a broken system-
metrics collector never aborts the cycle (fail-open, matching the rest of
Epic 4's "observability never perturbs trading" invariant)."""

from __future__ import annotations

from datetime import time
from pathlib import Path

from conftest import NOON_UTC, FakeMarketDataSource
from conftest import flat_candles as _flat_candles

from clav.clock import FakeClock
from clav.config import ObservabilityConfig
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.decision import DecisionEngine, Thresholds, Weights
from clav.domain.indicators import IndicatorService
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import TradingWindow, default_rules
from clav.domain.risk.sizing import PositionSizer
from clav.integrations.dryrun_broker import DryRunBroker
from clav.interfaces.system_metrics import SystemMetricsCollector
from clav.services.health_monitor import HealthMonitor
from clav.services.scan_cycle import ScanCycleService
from clav.services.stop_monitor import StopMonitor

WINDOW = TradingWindow(start=time(9, 35), end=time(15, 55), timezone="America/New_York")


class _FakeSystemMetrics(SystemMetricsCollector):
    def __init__(self, *, raise_on: str | None = None) -> None:
        self._raise_on = raise_on

    def process_rss_bytes(self) -> int:
        if self._raise_on == "process_rss_bytes":
            raise RuntimeError("psutil boom")
        return 100 * 1024 * 1024

    def free_memory_bytes(self) -> int:
        return 1024 * 1024 * 1024

    def cpu_load_pct(self) -> float:
        return 5.0

    def disk_free_bytes(self, path: Path) -> int:
        return 10 * 1024 * 1024 * 1024


def _session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _service(session_factory, data_source, *, clock, health_monitor) -> ScanCycleService:
    broker = DryRunBroker(clock=clock, market_open=True)
    return ScanCycleService(
        watchlist=["MSFT"],
        data_source=data_source,
        indicators=IndicatorService(),
        decision_engine=DecisionEngine(
            Weights(technical=1.0, llm=0.0, portfolio=0.0),
            Thresholds(buy=0.2, sell=-0.2),
            default_order_value=1000.0,
            clock=clock,
        ),
        risk_engine=RiskEngine(default_rules()),
        position_sizer=PositionSizer(
            risk_fraction=0.01,
            atr_stop_mult=2.0,
            take_profit_mult=2.0,
            default_order_value=1000.0,
        ),
        stop_monitor=StopMonitor(data_source, clock=clock, quote_staleness_seconds=300),
        broker=broker,
        session_factory=session_factory,
        clock=clock,
        trading_window=WINDOW,
        max_position_value=2000.0,
        buying_power_buffer_pct=0.05,
        max_portfolio_exposure_pct=0.80,
        max_sector_allocation_pct=0.30,
        max_daily_loss_pct=0.03,
        max_drawdown_pct=0.10,
        min_avg_volume=0.0,
        earnings_blackout_days=2,
        cooldown_minutes=60,
        post_loss_cooldown_minutes=120,
        mode="dryrun",
        health_monitor=health_monitor,
    )


def test_full_cycle_writes_health_events_across_categories(tmp_path) -> None:
    clock = FakeClock(NOON_UTC)
    session_factory = _session_factory(tmp_path)
    data_source = FakeMarketDataSource(
        {"MSFT": _flat_candles("MSFT", n=60)}, clock=clock, market_open=True
    )
    health_monitor = HealthMonitor(
        clock=clock,
        system_metrics=_FakeSystemMetrics(),
        db_path=tmp_path / "clav.db",
        thresholds=ObservabilityConfig(),
    )
    service = _service(session_factory, data_source, clock=clock, health_monitor=health_monitor)
    service.startup_reconcile()
    cycle_id = service.run()

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        cycle = repos.scan_cycles.get(cycle_id)
        assert cycle is not None
        assert cycle.status == "completed"

        events = repos.health_events.list_recent(limit=100)
        categories = {e.category for e in events}
        assert {"freshness", "external", "system", "trading", "liveness"} <= categories

        snapshot = repos.system_control.get("health_snapshot")
        assert snapshot is not None


def test_health_monitor_failure_never_aborts_cycle(tmp_path) -> None:
    """A system-metrics collector that raises on every call must not stop the
    cycle from completing (observability is never on the critical path)."""
    clock = FakeClock(NOON_UTC)
    session_factory = _session_factory(tmp_path)
    data_source = FakeMarketDataSource(
        {"MSFT": _flat_candles("MSFT", n=60)}, clock=clock, market_open=True
    )
    health_monitor = HealthMonitor(
        clock=clock,
        system_metrics=_FakeSystemMetrics(raise_on="process_rss_bytes"),
        db_path=tmp_path / "clav.db",
        thresholds=ObservabilityConfig(),
    )
    service = _service(session_factory, data_source, clock=clock, health_monitor=health_monitor)
    cycle_id = service.run()

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        cycle = repos.scan_cycles.get(cycle_id)
        assert cycle is not None
        assert cycle.status == "completed"
