"""Story 4.1 acceptance: HealthMonitor writes durable health_event rows for
freshness/external/system/trading/liveness, never aborts on a collector
failure, and persists the compact health_snapshot for clav-web to read."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clav.clock import FakeClock
from clav.config import ObservabilityConfig
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import (
    Candle,
    IndicatorSet,
    NewsItem,
    PortfolioSnapshot,
    SocialDigest,
)
from clav.interfaces.system_metrics import SystemMetricsCollector
from clav.services.health_monitor import HealthMonitor

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


class SpyAlerter:
    """Duck-typed stand-in for ``Alerter`` — records ``notify`` calls without
    needing real channels; ``Alerter``'s own dispatch/dedup/digest behavior
    is covered separately in ``test_alerting.py``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict[str, object]]] = []
        self.ticks = 0

    def notify(
        self, condition: str, severity: str, message: str, context: dict[str, object] | None = None
    ) -> None:
        self.calls.append((condition, severity, message, context or {}))

    def tick(self) -> None:
        self.ticks += 1


class FakeSystemMetricsCollector(SystemMetricsCollector):
    def __init__(
        self,
        *,
        rss_bytes: int = 100 * 1024 * 1024,
        free_memory_bytes: int = 1024 * 1024 * 1024,
        cpu_load_pct: float = 10.0,
        disk_free_bytes: int = 10 * 1024 * 1024 * 1024,
        raise_on: str | None = None,
    ) -> None:
        self._rss_bytes = rss_bytes
        self._free_memory_bytes = free_memory_bytes
        self._cpu_load_pct = cpu_load_pct
        self._disk_free_bytes = disk_free_bytes
        self._raise_on = raise_on

    def process_rss_bytes(self) -> int:
        if self._raise_on == "process_rss_bytes":
            raise RuntimeError("psutil boom")
        return self._rss_bytes

    def free_memory_bytes(self) -> int:
        if self._raise_on == "free_memory_bytes":
            raise RuntimeError("psutil boom")
        return self._free_memory_bytes

    def cpu_load_pct(self) -> float:
        if self._raise_on == "cpu_load_pct":
            raise RuntimeError("psutil boom")
        return self._cpu_load_pct

    def disk_free_bytes(self, path: Path) -> int:
        if self._raise_on == "disk_free_bytes":
            raise RuntimeError("psutil boom")
        return self._disk_free_bytes


def _thresholds(**overrides: object) -> ObservabilityConfig:
    defaults: dict[str, object] = {
        "freshness_warn_hours": 6.0,
        "freshness_critical_hours": 24.0,
        "free_memory_warn_mb": 256.0,
        "free_memory_critical_mb": 100.0,
        "disk_free_warn_mb": 512.0,
        "disk_free_critical_mb": 150.0,
        "cpu_warn_pct": 85.0,
        "cpu_critical_pct": 97.0,
        "retention_per_category": 500,
    }
    defaults.update(overrides)
    return ObservabilityConfig(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _monitor(
    tmp_path,
    *,
    clock: FakeClock | None = None,
    system_metrics: SystemMetricsCollector | None = None,
    thresholds: ObservabilityConfig | None = None,
    alerter: SpyAlerter | None = None,
) -> HealthMonitor:
    return HealthMonitor(
        clock=clock or FakeClock(NOW),
        system_metrics=system_metrics or FakeSystemMetricsCollector(),
        db_path=tmp_path / "clav.db",
        thresholds=thresholds or _thresholds(),
        alerter=alerter,  # type: ignore[arg-type]
    )


def _base_portfolio_snapshot(
    *, equity: float = 10_000.0, drawdown: float = 0.0
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        ts=NOW, cash=equity, equity=equity, buying_power=equity, drawdown=drawdown
    )


def _events_by_name(events, category: str) -> dict[str, object]:
    return {e.name: e for e in events if e.category == category}


def _seed_cycle(repos: Repositories, cycle_id: str) -> None:
    """health_event.cycle_id FKs to scan_cycle.id — matching real usage,
    where ScanCycleService always creates the scan_cycle row before
    HealthMonitor ever runs."""
    repos.scan_cycles.create(cycle_id, started_at=NOW, mode="dryrun", trigger="scheduled")


# --- freshness ---------------------------------------------------------------


def test_freshness_ok_for_recent_data(session_factory, tmp_path) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        recent = NOW - timedelta(hours=1)
        repos.candles.upsert_many(
            instrument.id,
            [
                Candle(
                    symbol="AAPL",
                    timeframe="1Day",
                    open=1,
                    high=2,
                    low=0.5,
                    close=1.5,
                    volume=1,
                    ts=recent,
                )
            ],
        )
        repos.indicator_sets.add(instrument.id, IndicatorSet(symbol="AAPL", ts=recent, close=1.5))
        repos.news_items.add_many(
            instrument.id,
            [
                NewsItem(
                    id="n1",
                    symbol="AAPL",
                    headline="headline",
                    source="rss",
                    published_at=recent,
                    fetched_at=recent,
                )
            ],
        )
        repos.social_digests.add(instrument.id, SocialDigest(symbol="AAPL", generated_at=recent))
        _seed_cycle(repos, "c1")

        monitor = _monitor(tmp_path)
        events = monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=["AAPL"],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    freshness = _events_by_name(events, "freshness")
    assert freshness["AAPL:candle"].status == "ok"
    assert freshness["AAPL:indicator"].status == "ok"
    assert freshness["AAPL:news"].status == "ok"
    assert freshness["AAPL:social"].status == "ok"


def test_freshness_warn_and_critical_boundaries(session_factory, tmp_path) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        warn_age = NOW - timedelta(hours=6)  # exactly at warn threshold
        critical_age = NOW - timedelta(hours=24)  # exactly at critical threshold
        repos.candles.upsert_many(
            instrument.id,
            [
                Candle(
                    symbol="AAPL",
                    timeframe="1Day",
                    open=1,
                    high=2,
                    low=0.5,
                    close=1.5,
                    volume=1,
                    ts=warn_age,
                )
            ],
        )
        repos.indicator_sets.add(
            instrument.id, IndicatorSet(symbol="AAPL", ts=critical_age, close=1.5)
        )
        _seed_cycle(repos, "c1")

        monitor = _monitor(tmp_path)
        events = monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=["AAPL"],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    freshness = _events_by_name(events, "freshness")
    assert freshness["AAPL:candle"].status == "warn"
    assert freshness["AAPL:indicator"].status == "critical"
    # never observed -> warn, not critical/ok, and age_hours is None
    assert freshness["AAPL:news"].status == "warn"
    assert freshness["AAPL:news"].value["age_hours"] is None


def test_freshness_skips_symbols_never_seen(session_factory, tmp_path) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path)
        events = monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=["ZZZZ"],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    assert _events_by_name(events, "freshness") == {}


# --- external ------------------------------------------------------------


def test_external_alpaca_down_is_critical(session_factory, tmp_path) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path)
        events = monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=False,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    assert _events_by_name(events, "external")["alpaca"].status == "critical"


@pytest.mark.parametrize(
    ("breaker_open", "budget_exhausted", "expected"),
    [
        (False, False, "ok"),
        (False, True, "warn"),
        (True, False, "critical"),
        (True, True, "critical"),
    ],
)
def test_external_gemini_status_from_budget_snapshot(
    session_factory, tmp_path, breaker_open, budget_exhausted, expected
) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path)
        events = monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot={
                "breaker_open": breaker_open,
                "budget_exhausted": budget_exhausted,
            },
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    assert _events_by_name(events, "external")["gemini"].status == expected


def test_external_gemini_absent_when_no_analyst_configured(session_factory, tmp_path) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path)
        events = monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    assert "gemini" not in _events_by_name(events, "external")


# --- system ------------------------------------------------------------


def test_system_memory_disk_cpu_thresholds(session_factory, tmp_path) -> None:
    collector = FakeSystemMetricsCollector(
        free_memory_bytes=50 * 1024 * 1024,  # below critical_mb=100
        disk_free_bytes=200 * 1024 * 1024,  # between warn=512 and critical=150 -> warn
        cpu_load_pct=99.0,  # above critical=97
    )
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path, system_metrics=collector)
        events = monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    system = _events_by_name(events, "system")
    assert system["free_memory"].status == "critical"
    assert system["disk_free"].status == "warn"
    assert system["cpu_load"].status == "critical"
    assert system["process_rss"].status == "ok"


def test_collector_failure_is_isolated_and_never_raises(session_factory, tmp_path) -> None:
    collector = FakeSystemMetricsCollector(raise_on="cpu_load_pct")
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path, system_metrics=collector)
        events = monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    system = _events_by_name(events, "system")
    assert system["collector_error"].status == "warn"
    assert "psutil boom" in system["collector_error"].value["error"]
    # other collectors still ran despite the system collector failing
    assert _events_by_name(events, "liveness")["last_successful_cycle"].status == "ok"


# --- trading -------------------------------------------------------------


def test_trading_counts_drawdown_and_daily_pnl(session_factory, tmp_path) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        repos.scan_cycles.create("c1", started_at=NOW, mode="dryrun", trigger="scheduled")
        repos.decisions.add(
            scan_cycle_id="c1",
            instrument_id=instrument.id,
            decision=_FakeDecision(action="BUY"),
            created_at=NOW,
        )
        repos.decisions.add(
            scan_cycle_id="c1",
            instrument_id=instrument.id,
            decision=_FakeDecision(action="HOLD"),
            created_at=NOW,
        )

        # daily loss of 5% against a 3% cap -> critical
        snapshot = _base_portfolio_snapshot(equity=9_500.0, drawdown=0.15)
        monitor = _monitor(tmp_path)
        events = monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=["AAPL"],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=snapshot,
            daily_start_equity=10_000.0,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    trading = _events_by_name(events, "trading")
    assert trading["decisions_by_action"].value["counts"] == {"BUY": 1, "HOLD": 1}
    assert trading["drawdown"].status == "critical"  # 0.15 >= cap 0.10
    assert trading["daily_pnl_vs_cap"].status == "critical"  # -5% loss >= 3% cap
    assert trading["daily_pnl_vs_cap"].value["daily_pnl_pct"] == pytest.approx(-0.05)


def test_daily_pnl_ok_when_no_start_equity_recorded(session_factory, tmp_path) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.scan_cycles.create("c1", started_at=NOW, mode="dryrun", trigger="scheduled")
        monitor = _monitor(tmp_path)
        events = monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    trading = _events_by_name(events, "trading")
    assert trading["daily_pnl_vs_cap"].status == "ok"
    assert trading["daily_pnl_vs_cap"].value["daily_pnl_pct"] is None


class _FakeDecision:
    """Minimal stand-in for TradeDecision — DecisionRepository.add only reads
    these attributes off whatever it's given."""

    def __init__(self, *, action: str) -> None:
        self.action = action
        self.raw_score = 0.0
        self.technical_score = 0.0
        self.llm_signal = 0.0
        self.portfolio_bias = 0.0
        self.target_qty = 0
        self.reasoning: dict[str, object] = {}


# --- liveness + snapshot ---------------------------------------------------


def test_liveness_event_recorded_on_success(session_factory, tmp_path) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path)
        events = monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    liveness = _events_by_name(events, "liveness")["last_successful_cycle"]
    assert liveness.status == "ok"
    assert liveness.value["cycle_id"] == "c1"


def test_health_snapshot_persisted_to_system_control(session_factory, tmp_path) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path)
        monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )
        raw = repos.system_control.get("health_snapshot")

    assert raw is not None
    snapshot = json.loads(raw)
    assert "liveness" in snapshot["categories"]
    assert snapshot["categories"]["liveness"]["last_successful_cycle"]["status"] == "ok"


def test_record_startup_writes_system_and_process_started(session_factory, tmp_path) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path)
        events = monitor.record_startup(repos)

    liveness = _events_by_name(events, "liveness")
    assert liveness["process_started"].status == "ok"
    assert _events_by_name(events, "system") != {}


# --- retention -------------------------------------------------------------


def test_prune_bounds_rows_per_category(session_factory, tmp_path) -> None:
    thresholds = _thresholds(retention_per_category=2)
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        clock = FakeClock(NOW)
        monitor = _monitor(tmp_path, clock=clock, thresholds=thresholds)
        for i in range(5):
            clock.advance(timedelta(minutes=1))
            _seed_cycle(repos, f"c{i}")
            monitor.run_cycle_end(
                repos,
                cycle_id=f"c{i}",
                watchlist=[],
                alpaca_ok=True,
                llm_budget_snapshot=None,
                portfolio_snapshot=_base_portfolio_snapshot(),
                daily_start_equity=None,
                max_daily_loss_pct=0.03,
                max_drawdown_pct=0.10,
            )
        remaining = repos.health_events.list_recent(category="liveness", limit=100)

    assert len(remaining) == 2


# --- Story 4.3: alert trigger evaluation ------------------------------------


def test_memory_pressure_alert_matches_health_event_severity(session_factory, tmp_path) -> None:
    spy = SpyAlerter()
    collector = FakeSystemMetricsCollector(free_memory_bytes=50 * 1024 * 1024)  # < critical_mb=100
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path, system_metrics=collector, alerter=spy)
        monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    conditions = {c[0]: c for c in spy.calls}
    assert conditions["memory_pressure"][1] == "critical"


def test_disk_pressure_alert_warning_not_critical(session_factory, tmp_path) -> None:
    spy = SpyAlerter()
    # 200MB is below warn=512 but above critical=150 -> "warn" -> "warning" alert.
    collector = FakeSystemMetricsCollector(disk_free_bytes=200 * 1024 * 1024)
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path, system_metrics=collector, alerter=spy)
        monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    conditions = {c[0]: c for c in spy.calls}
    assert conditions["disk_pressure"][1] == "warning"


def test_broker_unreachable_alert_when_alpaca_critical(session_factory, tmp_path) -> None:
    spy = SpyAlerter()
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path, alerter=spy)
        monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=False,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    conditions = {c[0]: c for c in spy.calls}
    assert conditions["broker_unreachable"][1] == "critical"


def test_llm_breaker_open_alert(session_factory, tmp_path) -> None:
    spy = SpyAlerter()
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path, alerter=spy)
        monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot={"breaker_open": True, "budget_exhausted": False},
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    conditions = {c[0]: c for c in spy.calls}
    assert conditions["llm_breaker_open"][1] == "critical"
    assert "llm_budget_exhausted" not in conditions


def test_llm_budget_exhausted_alert_is_warning(session_factory, tmp_path) -> None:
    spy = SpyAlerter()
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path, alerter=spy)
        monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot={"breaker_open": False, "budget_exhausted": True},
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    conditions = {c[0]: c for c in spy.calls}
    assert conditions["llm_budget_exhausted"][1] == "warning"


def test_drawdown_breach_alert(session_factory, tmp_path) -> None:
    spy = SpyAlerter()
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path, alerter=spy)
        monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(drawdown=0.15),  # >= cap 0.10
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    conditions = {c[0]: c for c in spy.calls}
    assert conditions["drawdown_breach"][1] == "critical"


def test_daily_loss_cap_hit_alert(session_factory, tmp_path) -> None:
    spy = SpyAlerter()
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path, alerter=spy)
        monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(equity=9_500.0),  # -5% vs cap 3%
            daily_start_equity=10_000.0,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    conditions = {c[0]: c for c in spy.calls}
    assert conditions["daily_loss_cap_hit"][1] == "critical"


def test_emergency_stop_alert_fires_only_on_the_edge(session_factory, tmp_path) -> None:
    spy = SpyAlerter()
    clock = FakeClock(NOW)
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        monitor = _monitor(tmp_path, clock=clock, alerter=spy)

        _seed_cycle(repos, "c1")
        monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
            emergency_stop=True,
        )
        first_conditions = [c[0] for c in spy.calls]
        assert first_conditions.count("emergency_stop_tripped") == 1

        clock.advance(timedelta(minutes=1))
        _seed_cycle(repos, "c2")
        monitor.run_cycle_end(
            repos,
            cycle_id="c2",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
            emergency_stop=True,
        )

    # still tripped on cycle 2 -- no *new* edge, so no second alert.
    second_conditions = [c[0] for c in spy.calls]
    assert second_conditions.count("emergency_stop_tripped") == 1


def test_emergency_stop_alert_fires_again_after_clearing_and_retripping(
    session_factory, tmp_path
) -> None:
    spy = SpyAlerter()
    clock = FakeClock(NOW)
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        monitor = _monitor(tmp_path, clock=clock, alerter=spy)

        for cid, estop in (("c1", True), ("c2", False), ("c3", True)):
            clock.advance(timedelta(minutes=1))
            _seed_cycle(repos, cid)
            monitor.run_cycle_end(
                repos,
                cycle_id=cid,
                watchlist=[],
                alpaca_ok=True,
                llm_budget_snapshot=None,
                portfolio_snapshot=_base_portfolio_snapshot(),
                daily_start_equity=None,
                max_daily_loss_pct=0.03,
                max_drawdown_pct=0.10,
                emergency_stop=estop,
            )

    conditions = [c[0] for c in spy.calls]
    assert conditions.count("emergency_stop_tripped") == 2


def test_cycle_gap_alert_only_during_market_hours(session_factory, tmp_path) -> None:
    spy = SpyAlerter()
    clock = FakeClock(NOW)
    thresholds = _thresholds(max_cycle_gap_minutes=30)
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        monitor = _monitor(tmp_path, clock=clock, thresholds=thresholds, alerter=spy)

        _seed_cycle(repos, "c1")
        monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
            market_open=True,
        )
        # No previous liveness event yet -- must not false-alarm on cycle 1.
        assert "cycle_gap_exceeded" not in [c[0] for c in spy.calls]

        clock.advance(timedelta(minutes=45))  # > 30 min cap
        _seed_cycle(repos, "c2")
        monitor.run_cycle_end(
            repos,
            cycle_id="c2",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
            market_open=False,  # market closed -- a long gap here is expected
        )
        assert "cycle_gap_exceeded" not in [c[0] for c in spy.calls]

        clock.advance(timedelta(minutes=45))
        _seed_cycle(repos, "c3")
        monitor.run_cycle_end(
            repos,
            cycle_id="c3",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
            market_open=True,
        )

    assert "cycle_gap_exceeded" in [c[0] for c in spy.calls]


def test_alert_events_persisted_as_alert_category(session_factory, tmp_path) -> None:
    spy = SpyAlerter()
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path, alerter=spy)
        monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=False,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )
        alert_events = repos.health_events.list_recent(category="alert")

    assert any(e.name == "broker_unreachable" and e.status == "critical" for e in alert_events)


def test_alerter_tick_called_once_per_cycle(session_factory, tmp_path) -> None:
    spy = SpyAlerter()
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path, alerter=spy)
        monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=True,
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    assert spy.ticks == 1


def test_no_alerts_evaluated_without_an_alerter_configured(session_factory, tmp_path) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        _seed_cycle(repos, "c1")
        monitor = _monitor(tmp_path)  # no alerter
        events = monitor.run_cycle_end(
            repos,
            cycle_id="c1",
            watchlist=[],
            alpaca_ok=False,  # would otherwise be alert-worthy
            llm_budget_snapshot=None,
            portfolio_snapshot=_base_portfolio_snapshot(),
            daily_start_equity=None,
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.10,
        )

    assert _events_by_name(events, "alert") == {}
