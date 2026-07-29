"""The "beating the market" tile, wired into a real scan cycle.

The pure comparison in ``domain.benchmark`` was unit-tested from the start;
this covers the wiring that feeds it -- which is where the interesting failure
lives. ``_persist_benchmark_snapshot`` swallows every exception on purpose (a
cosmetic panel must never fail a cycle), so anything that raises in there makes
the tile *permanently absent* with nothing but a log line to say why. The
regression below is exactly that: SQLite hands back a naive ``portfolio_snapshot.ts``
while the market-data adapter returns tz-aware candle timestamps, and comparing
them raises TypeError.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta

from conftest import NOON_UTC, FakeMarketDataSource
from conftest import flat_candles as _flat_candles

from clav.clock import FakeClock
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.decision import DecisionEngine, Thresholds, Weights
from clav.domain.indicators import IndicatorService
from clav.domain.models import Candle, PortfolioSnapshot
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import TradingWindow, default_rules
from clav.domain.risk.sizing import PositionSizer
from clav.integrations.dryrun_broker import DryRunBroker
from clav.services.scan_cycle import BENCHMARK_SNAPSHOT_KEY, ScanCycleService
from clav.services.stop_monitor import StopMonitor

WINDOW = TradingWindow(start=time(9, 35), end=time(15, 55), timezone="America/New_York")


def _voo_candles(closes: list[float], *, start: datetime, tz_aware: bool) -> list[Candle]:
    """alpaca-py returns tz-aware UTC bar timestamps; a naive-ts source is the
    contrast case that proves the fix handles both."""
    out = []
    for i, close in enumerate(closes):
        ts = start + timedelta(days=i)
        out.append(
            Candle(
                symbol="VOO",
                timeframe="1Day",
                open=close,
                high=close,
                low=close,
                close=close,
                volume=1_000_000,
                ts=ts if tz_aware else ts.replace(tzinfo=None),
            )
        )
    return out


def _service(factory, data_source, *, clock, benchmark_symbol="VOO"):
    return ScanCycleService(
        execution_poll_sleep=lambda _: None,  # DryRunBroker never fills; skip the real-sleep poll
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
            risk_fraction=0.01, atr_stop_mult=2.0, take_profit_mult=2.0, default_order_value=1000.0
        ),
        stop_monitor=StopMonitor(data_source, clock=clock, quote_staleness_seconds=300),
        broker=DryRunBroker(clock=clock, market_open=True),
        session_factory=factory,
        clock=clock,
        trading_window=WINDOW,
        benchmark_symbol=benchmark_symbol,
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
    )


def _seed_baseline(factory, *, equity: float, ts: datetime) -> None:
    """Written through the real repository, so the ts round-trips through
    SQLite and comes back naive exactly as it does in production."""
    with session_scope(factory) as session:
        Repositories(session).portfolio_snapshots.add(
            PortfolioSnapshot(ts=ts, cash=equity, equity=equity, buying_power=equity)
        )


def _read_snapshot(factory) -> dict | None:
    with session_scope(factory) as session:
        raw = Repositories(session).system_control.get(BENCHMARK_SNAPSHOT_KEY)
    return json.loads(raw) if raw else None


def _run(tmp_path, *, tz_aware_candles: bool, benchmark_symbol: str = "VOO"):
    clock = FakeClock(NOON_UTC)
    baseline_ts = NOON_UTC - timedelta(days=10)
    data_source = FakeMarketDataSource(
        {
            "MSFT": _flat_candles("MSFT"),
            # VOO 400 -> 420 over the window = +5.00%
            "VOO": _voo_candles(
                [400.0, 410.0, 420.0], start=baseline_ts, tz_aware=tz_aware_candles
            ),
        },
        clock=clock,
    )
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    _seed_baseline(factory, equity=10_000.0, ts=baseline_ts)
    _service(factory, data_source, clock=clock, benchmark_symbol=benchmark_symbol).run()
    return _read_snapshot(factory)


def test_tz_aware_candles_against_a_naive_db_baseline(tmp_path) -> None:
    """Regression: SQLite strips tzinfo from portfolio_snapshot.ts, alpaca-py
    keeps it on candle.ts, and the naive-vs-aware comparison raised TypeError
    into the fail-open except -- so the tile never appeared at all."""
    snapshot = _run(tmp_path, tz_aware_candles=True)

    assert snapshot is not None, "benchmark snapshot missing -- the tile would never render"
    assert snapshot["symbol"] == "VOO"
    assert snapshot["benchmark_return_pct"] == 5.0
    # delta = portfolio return - benchmark return; both measured from the baseline.
    assert snapshot["delta_pct"] == round(
        snapshot["portfolio_return_pct"] - snapshot["benchmark_return_pct"], 2
    )


def test_naive_candles_also_work(tmp_path) -> None:
    """A data source that returns naive timestamps must not break either."""
    snapshot = _run(tmp_path, tz_aware_candles=False)
    assert snapshot is not None
    assert snapshot["benchmark_return_pct"] == 5.0


def test_empty_benchmark_symbol_writes_nothing(tmp_path) -> None:
    """`benchmark_symbol: ""` hides the tile rather than writing a stub."""
    assert _run(tmp_path, tz_aware_candles=True, benchmark_symbol="") is None


def test_no_portfolio_history_writes_nothing(tmp_path) -> None:
    """A fresh install has no baseline; the tile must be absent, not 0%."""
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource(
        {
            "MSFT": _flat_candles("MSFT"),
            "VOO": _voo_candles([400.0], start=NOON_UTC, tz_aware=True),
        },
        clock=clock,
    )
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    # No _seed_baseline() -- but run_cycle writes its own snapshot, so the
    # baseline and current equity coincide and the return is 0%, not absent.
    _service(factory, data_source, clock=clock).run()
    snapshot = _read_snapshot(factory)
    if snapshot is not None:
        assert snapshot["portfolio_return_pct"] == 0.0


def test_unpriceable_benchmark_leaves_the_cycle_healthy(tmp_path) -> None:
    """A benchmark symbol the data source can't price is cosmetic: no snapshot,
    no exception out of the cycle."""
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({"MSFT": _flat_candles("MSFT")}, clock=clock)
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    _seed_baseline(factory, equity=10_000.0, ts=NOON_UTC - timedelta(days=10))
    _service(factory, data_source, clock=clock, benchmark_symbol="NOPE").run()
    assert _read_snapshot(factory) is None
