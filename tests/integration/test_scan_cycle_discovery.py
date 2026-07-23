"""Autonomous discovery + on-demand analysis wired into a full scan cycle:
a discovered name and an on-demand request both flow through the identical
_process_symbol path (decision + risk gate), and the on-demand request is
linked to its decision."""

from __future__ import annotations

from datetime import time

from conftest import NOON_UTC, FakeMarketDataSource
from conftest import flat_candles as _flat_candles

from clav.clock import FakeClock
from clav.common.cache import TtlCache
from clav.data import tables
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.decision import DecisionEngine, Thresholds, Weights
from clav.domain.indicators import IndicatorService
from clav.domain.models import DiscoveryCandidate
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import TradingWindow, default_rules
from clav.domain.risk.sizing import PositionSizer
from clav.domain.social import SocialFilterParams
from clav.integrations.dryrun_broker import DryRunBroker
from clav.interfaces.analyst import AnalystSignal
from clav.interfaces.discovery import DiscoverySource
from clav.services.analyst_gateway import AnalystGateway
from clav.services.discovery import DISCOVERY_SNAPSHOT_KEY, DiscoveryService
from clav.services.scan_cycle import ScanCycleService
from clav.services.stop_monitor import StopMonitor

WINDOW = TradingWindow(start=time(9, 35), end=time(15, 55), timezone="America/New_York")


class StaticAnalyst:
    def __init__(self, signal: AnalystSignal) -> None:
        self._signal = signal

    def analyze(self, symbol, news, social_digest, context) -> AnalystSignal:
        return self._signal


class StaticDiscovery(DiscoverySource):
    def __init__(self, candidates: list[DiscoveryCandidate]) -> None:
        self._c = candidates

    def fetch(self) -> list[DiscoveryCandidate]:
        return self._c


def _gateway(clock, signal: AnalystSignal) -> AnalystGateway:
    return AnalystGateway(
        analyst=StaticAnalyst(signal),
        news_sources=[],
        social_sources=[],
        filter_params=SocialFilterParams(),
        clock=clock,
        cache=TtlCache(clock=clock, ttl_seconds=3600),
        max_age_hours=72,
        max_items_per_symbol=50,
        social_baseline_window=20,
    )


def _service(factory, data_source, *, clock, gateway, discovery_service):
    return ScanCycleService(
        watchlist=["MSFT"],
        data_source=data_source,
        indicators=IndicatorService(),
        decision_engine=DecisionEngine(
            Weights(technical=0.0, llm=1.0, portfolio=0.0),
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
        analyst_gateway=gateway,
        discovery_service=discovery_service,
        discovery_enabled=True,
        on_demand_enabled=True,
        on_demand_max_per_cycle=5,
    )


def test_discovered_and_on_demand_symbols_get_decisions(tmp_path) -> None:
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource(
        {
            "MSFT": _flat_candles("MSFT"),
            "NVDA": _flat_candles("NVDA"),
            "TSLA": _flat_candles("TSLA"),
        },
        clock=clock,
    )
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    # An operator on-demand request for TSLA, queued before the cycle runs.
    with session_scope(factory) as session:
        Repositories(session).analysis_requests.create(
            "TSLA", requested_by="operator", requested_at=clock.now()
        )

    signal = AnalystSignal(sentiment=0.9, conviction=0.9, rationale="buzz", model="fake")
    discovery = DiscoveryService(
        [StaticDiscovery([DiscoveryCandidate(symbol="NVDA", score=0.9, source="fake")])],
        clock=clock,
    )
    service = _service(
        factory, data_source, clock=clock, gateway=_gateway(clock, signal),
        discovery_service=discovery,
    )

    service.run(trigger="manual")

    with session_scope(factory) as session:
        repos = Repositories(session)
        # all three symbols -- pin, discovered, on-demand -- produced a decision
        for symbol in ["MSFT", "NVDA", "TSLA"]:
            inst = repos.instruments.get_by_symbol(symbol)
            assert inst is not None, symbol
            decision = (
                session.query(tables.Decision).filter_by(instrument_id=inst.id).first()
            )
            assert decision is not None, f"no decision for {symbol}"

        # the on-demand request is marked done and linked to its decision
        req = repos.analysis_requests.list_recent(limit=5)[0]
        assert req.symbol == "TSLA"
        assert req.status == "done"
        assert req.decision_id is not None

        # discovery recorded a snapshot for the UI
        assert repos.system_control.get(DISCOVERY_SNAPSHOT_KEY) is not None


def test_discovery_off_leaves_universe_as_watchlist(tmp_path) -> None:
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource(
        {"MSFT": _flat_candles("MSFT"), "NVDA": _flat_candles("NVDA")}, clock=clock
    )
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    signal = AnalystSignal(sentiment=0.0, conviction=0.0, rationale="", model="fake")
    discovery = DiscoveryService(
        [StaticDiscovery([DiscoveryCandidate(symbol="NVDA", score=0.9, source="fake")])],
        clock=clock,
    )
    service = _service(
        factory, data_source, clock=clock, gateway=_gateway(clock, signal),
        discovery_service=discovery,
    )
    service._discovery_enabled = False  # simulate discovery.enabled=False

    service.run(trigger="manual")

    with session_scope(factory) as session:
        repos = Repositories(session)
        # NVDA was never scanned -> no instrument row created for it
        assert repos.instruments.get_by_symbol("NVDA") is None
        assert repos.instruments.get_by_symbol("MSFT") is not None
