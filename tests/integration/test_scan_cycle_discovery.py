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


def _service(
    factory,
    data_source,
    *,
    clock,
    gateway,
    discovery_service,
    mode="dryrun",
    allow_live_discovery=False,
    market_open=True,
):
    return ScanCycleService(
        execution_poll_sleep=lambda _: None,  # DryRunBroker never fills; skip the real-sleep poll
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
        broker=DryRunBroker(clock=clock, market_open=market_open),
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
        mode=mode,
        analyst_gateway=gateway,
        discovery_service=discovery_service,
        discovery_enabled=True,
        allow_live_discovery=allow_live_discovery,
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
        factory,
        data_source,
        clock=clock,
        gateway=_gateway(clock, signal),
        discovery_service=discovery,
    )

    service.run(trigger="manual")

    with session_scope(factory) as session:
        repos = Repositories(session)
        # all three symbols -- pin, discovered, on-demand -- produced a decision
        for symbol in ["MSFT", "NVDA", "TSLA"]:
            inst = repos.instruments.get_by_symbol(symbol)
            assert inst is not None, symbol
            decision = session.query(tables.Decision).filter_by(instrument_id=inst.id).first()
            assert decision is not None, f"no decision for {symbol}"

        # the on-demand request is marked done and linked to its decision
        req = repos.analysis_requests.list_recent(limit=5)[0]
        assert req.symbol == "TSLA"
        assert req.status == "done"
        assert req.decision_id is not None

        # discovery recorded a snapshot for the UI
        assert repos.system_control.get(DISCOVERY_SNAPSHOT_KEY) is not None


def test_on_demand_request_runs_when_market_closed_but_watchlist_does_not(tmp_path) -> None:
    """The scheduled watchlist/discovery loop stays gated on market hours;
    an explicit on-demand request is a one-off question, not an automated
    trading decision, so it still gets answered overnight."""
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource(
        {"MSFT": _flat_candles("MSFT"), "TSLA": _flat_candles("TSLA")}, clock=clock
    )
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    with session_scope(factory) as session:
        Repositories(session).analysis_requests.create(
            "TSLA", requested_by="operator", requested_at=clock.now()
        )

    signal = AnalystSignal(sentiment=0.1, conviction=0.1, rationale="quiet", model="fake")
    service = _service(
        factory,
        data_source,
        clock=clock,
        gateway=_gateway(clock, signal),
        discovery_service=None,
        market_open=False,
    )

    cycle_id = service.run(trigger="manual")

    with session_scope(factory) as session:
        repos = Repositories(session)
        cycle = repos.scan_cycles.get(cycle_id)
        assert cycle is not None
        assert cycle.status == "on_demand_only"
        assert cycle.market_open is False

        # on-demand symbol got a real decision...
        tsla = repos.instruments.get_by_symbol("TSLA")
        assert tsla is not None
        decision = session.query(tables.Decision).filter_by(instrument_id=tsla.id).first()
        assert decision is not None

        # ...but the pinned watchlist symbol was never touched.
        assert repos.instruments.get_by_symbol("MSFT") is None

        req = repos.analysis_requests.list_recent(limit=5)[0]
        assert req.symbol == "TSLA"
        assert req.status == "done"
        assert req.decision_id is not None


def test_on_demand_buy_is_vetoed_not_silently_dropped_when_market_closed(tmp_path) -> None:
    """A bullish on-demand read still can't place a real order while the
    market's shut -- TradingHoursRule vetoes it, same as it would for any
    other BUY, rather than this path skipping the risk gate entirely."""
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({"MSFT": _flat_candles("MSFT")}, clock=clock)
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    with session_scope(factory) as session:
        Repositories(session).analysis_requests.create(
            "MSFT", requested_by="operator", requested_at=clock.now()
        )

    bullish = AnalystSignal(sentiment=0.9, conviction=0.9, rationale="buzz", model="fake")
    service = _service(
        factory,
        data_source,
        clock=clock,
        gateway=_gateway(clock, bullish),
        discovery_service=None,
        market_open=False,
    )

    service.run(trigger="manual")

    with session_scope(factory) as session:
        repos = Repositories(session)
        inst = repos.instruments.get_by_symbol("MSFT")
        assert inst is not None
        decision = session.query(tables.Decision).filter_by(instrument_id=inst.id).first()
        assert decision is not None and decision.action == "BUY"

        risk = session.query(tables.RiskEvaluation).filter_by(decision_id=decision.id).first()
        assert risk is not None
        assert risk.approved is False
        assert "TradingHoursRule" in risk.blocked_by

        assert (
            repos.orders.get_by_client_order_id(f"clav-{decision.scan_cycle_id}-MSFT-buy") is None
        )


def test_market_closed_with_no_pending_requests_still_skips(tmp_path) -> None:
    """Regression guard: on_demand_enabled alone must not turn every closed-
    market tick into a live cycle -- only an actual pending request should."""
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({"MSFT": _flat_candles("MSFT")}, clock=clock)
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    signal = AnalystSignal(sentiment=0.1, conviction=0.1, rationale="quiet", model="fake")
    service = _service(
        factory,
        data_source,
        clock=clock,
        gateway=_gateway(clock, signal),
        discovery_service=None,
        market_open=False,
    )

    cycle_id = service.run(trigger="manual")

    with session_scope(factory) as session:
        repos = Repositories(session)
        cycle = repos.scan_cycles.get(cycle_id)
        assert cycle is not None
        assert cycle.status == "skipped_market_closed"
        assert repos.instruments.get_by_symbol("MSFT") is None


def _run_live(tmp_path, *, allow_live: bool) -> bool:
    """Run one live-mode cycle with discovery enabled; return whether the
    discovered name (NVDA) was scanned."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource(
        {"MSFT": _flat_candles("MSFT"), "NVDA": _flat_candles("NVDA")}, clock=clock
    )
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    signal = AnalystSignal(sentiment=0.9, conviction=0.9, rationale="buzz", model="fake")
    discovery = DiscoveryService(
        [StaticDiscovery([DiscoveryCandidate(symbol="NVDA", score=0.9, source="fake")])],
        clock=clock,
    )
    service = _service(
        factory,
        data_source,
        clock=clock,
        gateway=_gateway(clock, signal),
        discovery_service=discovery,
        mode="live",
        allow_live_discovery=allow_live,
    )
    service.run(trigger="manual")
    with session_scope(factory) as session:
        return Repositories(session).instruments.get_by_symbol("NVDA") is not None


def test_live_mode_suppresses_discovery_without_allow_live(tmp_path) -> None:
    # Discovery is enabled, but the live interlock must keep the bot from
    # picking new names to trade with real money.
    assert _run_live(tmp_path / "a", allow_live=False) is False


def test_live_mode_allows_discovery_with_explicit_optin(tmp_path) -> None:
    assert _run_live(tmp_path / "b", allow_live=True) is True


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
        factory,
        data_source,
        clock=clock,
        gateway=_gateway(clock, signal),
        discovery_service=discovery,
    )
    service._discovery_enabled = False  # simulate discovery.enabled=False

    service.run(trigger="manual")

    with session_scope(factory) as session:
        repos = Repositories(session)
        # NVDA was never scanned -> no instrument row created for it
        assert repos.instruments.get_by_symbol("NVDA") is None
        assert repos.instruments.get_by_symbol("MSFT") is not None
