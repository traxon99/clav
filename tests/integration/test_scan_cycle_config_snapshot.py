"""Story 4.4 — ScanCycleService persists the *effective* config (boot config
+ any live RuntimeConfigStore override) once per cycle, joinable to a
decision via cycle_id, so any historical decision can be explained against
the exact settings that produced it (docs/10-observability.md §5)."""

from __future__ import annotations

from datetime import time

from conftest import NOON_UTC, FakeMarketDataSource
from conftest import flat_candles as _flat_candles

from clav.clock import FakeClock
from clav.config import RuntimeOverrides, WeightsConfig
from clav.data import tables
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.decision import DecisionEngine, Thresholds, Weights
from clav.domain.indicators import IndicatorService
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import TradingWindow, default_rules
from clav.domain.risk.sizing import PositionSizer
from clav.integrations.dryrun_broker import DryRunBroker
from clav.services.runtime_config import RuntimeConfigStore
from clav.services.scan_cycle import ScanCycleService
from clav.services.stop_monitor import StopMonitor

WINDOW = TradingWindow(start=time(9, 35), end=time(15, 55), timezone="America/New_York")

BASE_CONFIG = {
    "watchlist": ["MSFT"],
    "weights": {"technical": 1.0, "llm": 0.0, "portfolio": 0.0},
    "risk": {"max_position_value": 2000.0},
}


def _session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _service(
    session_factory, data_source, *, clock, runtime_config=None, git_sha="test-sha"
) -> ScanCycleService:
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
        runtime_config=runtime_config,
        config_snapshot_base=dict(BASE_CONFIG),
        git_sha=git_sha,
    )


def test_config_snapshot_persisted_each_cycle_and_dedups(tmp_path) -> None:
    clock = FakeClock(NOON_UTC)
    session_factory = _session_factory(tmp_path)
    data_source = FakeMarketDataSource(
        {"MSFT": _flat_candles("MSFT", n=60)}, clock=clock, market_open=True
    )
    service = _service(session_factory, data_source, clock=clock)

    cycle_1 = service.run()
    cycle_2 = service.run()

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        snap1 = repos.config_snapshots.get_by_cycle_id(cycle_1)
        snap2 = repos.config_snapshots.get_by_cycle_id(cycle_2)

        assert snap1 is not None
        assert snap2 is not None
        assert snap1.git_sha == "test-sha"
        assert snap1.config == BASE_CONFIG
        assert snap2.config == BASE_CONFIG  # resolved through the dedup pointer

        row2 = session.query(tables.ConfigSnapshotRow).filter_by(cycle_id=cycle_2).one()
        assert row2.config is None  # stored as a pointer, not a duplicate blob


def test_config_snapshot_captures_the_effective_runtime_override(tmp_path) -> None:
    clock = FakeClock(NOON_UTC)
    session_factory = _session_factory(tmp_path)
    data_source = FakeMarketDataSource(
        {"MSFT": _flat_candles("MSFT", n=60)}, clock=clock, market_open=True
    )
    runtime_config = RuntimeConfigStore()
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        runtime_config.set(
            repos,
            RuntimeOverrides(weights=WeightsConfig(technical=0.5, llm=0.5, portfolio=0.0)),
            now=clock.now(),
            updated_by="operator",
        )

    service = _service(session_factory, data_source, clock=clock, runtime_config=runtime_config)
    cycle_id = service.run()

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        snap = repos.config_snapshots.get_by_cycle_id(cycle_id)

    assert snap is not None
    # The override replaces the boot-config weights wholesale...
    assert snap.config["weights"] == {"technical": 0.5, "llm": 0.5, "portfolio": 0.0}
    # ...while untouched sections stay at their boot-config values.
    assert snap.config["watchlist"] == ["MSFT"]


def test_config_snapshot_joins_to_a_decision_via_cycle_id(tmp_path) -> None:
    clock = FakeClock(NOON_UTC)
    session_factory = _session_factory(tmp_path)
    data_source = FakeMarketDataSource(
        {"MSFT": _flat_candles("MSFT", n=60)}, clock=clock, market_open=True
    )
    service = _service(session_factory, data_source, clock=clock)
    cycle_id = service.run()

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        decision_row = session.query(tables.Decision).filter_by(scan_cycle_id=cycle_id).first()
        assert decision_row is not None
        snap = repos.config_snapshots.get_by_cycle_id(decision_row.scan_cycle_id)
        assert snap is not None
        assert snap.cycle_id == cycle_id


def test_unserializable_config_values_are_stringified_not_fatal(tmp_path) -> None:
    """A stray non-JSON-safe object in the base config (e.g. from a bug
    upstream) must never reach SQLAlchemy's JSON column directly -- it gets
    stringified by the json.dumps/loads round-trip in
    ``_persist_config_snapshot`` instead of blowing up mid-flush and
    poisoning the rest of the cycle's transaction."""
    clock = FakeClock(NOON_UTC)
    session_factory = _session_factory(tmp_path)
    data_source = FakeMarketDataSource(
        {"MSFT": _flat_candles("MSFT", n=60)}, clock=clock, market_open=True
    )
    service = _service(session_factory, data_source, clock=clock)
    service._config_snapshot_base = {"bad": object()}

    cycle_id = service.run()

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        cycle = repos.scan_cycles.get(cycle_id)
        assert cycle is not None
        assert cycle.status == "completed"

        snap = repos.config_snapshots.get_by_cycle_id(cycle_id)
        assert snap is not None
        assert isinstance(snap.config["bad"], str)  # stringified, not raw


def test_config_snapshot_repo_failure_never_aborts_the_cycle(tmp_path, monkeypatch) -> None:
    clock = FakeClock(NOON_UTC)
    session_factory = _session_factory(tmp_path)
    data_source = FakeMarketDataSource(
        {"MSFT": _flat_candles("MSFT", n=60)}, clock=clock, market_open=True
    )
    service = _service(session_factory, data_source, clock=clock)

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated config_snapshot repo failure")

    monkeypatch.setattr("clav.data.repositories.ConfigSnapshotRepository.add_for_cycle", _raise)

    cycle_id = service.run()

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        cycle = repos.scan_cycles.get(cycle_id)
        assert cycle is not None
        assert cycle.status == "completed"
        # the failed write never touched the session -- no row exists.
        assert repos.config_snapshots.get_by_cycle_id(cycle_id) is None
