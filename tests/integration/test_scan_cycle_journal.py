"""Story 3.7 — decision journal wired into a full scan cycle: auto mode
executes + journals every decision; manual mode holds a BUY pending until an
operator approves it via DecisionJournal (the Story 3.8 API's future backend)."""

from __future__ import annotations

from datetime import time

from conftest import NOON_UTC, FakeMarketDataSource
from conftest import flat_candles as _flat_candles
from conftest import trending_candles as _trending_candles

from clav.clock import FakeClock
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.decision import DecisionEngine, Thresholds, Weights
from clav.domain.indicators import IndicatorService
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import TradingWindow, default_rules
from clav.domain.risk.sizing import PositionSizer
from clav.integrations.dryrun_broker import DryRunBroker
from clav.services.decision_journal import ApprovalPolicy, DecisionJournal
from clav.services.execution import ExecutionEngine
from clav.services.scan_cycle import ScanCycleService
from clav.services.stop_monitor import StopMonitor

WINDOW = TradingWindow(start=time(9, 35), end=time(15, 55), timezone="America/New_York")


def _session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _service(session_factory, data_source, *, approval_policy, clock) -> ScanCycleService:
    broker = DryRunBroker(clock=clock, market_open=True)
    return ScanCycleService(
        watchlist=["AAPL"],
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
        approval_policy=approval_policy,
    )


def test_default_auto_mode_journals_every_decision(tmp_path) -> None:
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({"AAPL": _trending_candles("AAPL")}, clock=clock)
    factory = _session_factory(tmp_path)
    service = _service(factory, data_source, approval_policy=None, clock=clock)

    cycle_id = service.run(trigger="manual")

    with session_scope(factory) as session:
        repos = Repositories(session)
        order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-AAPL-buy")
        assert order is not None

        journal = repos.trade_proposals.list_recent(limit=10)
        assert len(journal) == 1
        assert journal[0].status == "executed"
        assert journal[0].symbol == "AAPL"
        assert journal[0].side == "buy"


def test_manual_mode_holds_buy_pending_then_approve_executes(tmp_path) -> None:
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({"AAPL": _trending_candles("AAPL")}, clock=clock)
    factory = _session_factory(tmp_path)
    policy = ApprovalPolicy(mode="manual", ttl_minutes=30)
    service = _service(factory, data_source, approval_policy=policy, clock=clock)

    cycle_id = service.run(trigger="manual")

    with session_scope(factory) as session:
        repos = Repositories(session)
        # No order yet -- entry held pending an operator decision.
        order = repos.orders.get_by_client_order_id(f"clav-{cycle_id}-AAPL-buy")
        assert order is None

        pending = repos.trade_proposals.list_pending()
        assert len(pending) == 1
        proposal_id = pending[0].id

    # Operator approves via the journal (Story 3.8's future API backend).
    with session_scope(factory) as session:
        repos = Repositories(session)
        broker = DryRunBroker(clock=clock, market_open=True)
        execution = ExecutionEngine(broker, repos, clock=clock)
        journal = DecisionJournal(repos=repos, execution=execution, clock=clock, policy=policy)
        result = journal.approve(proposal_id, decided_by="operator")
        assert result.order is not None
        assert result.proposal.status == "executed"


def test_web_marked_approval_executes_on_the_next_scan_cycle(tmp_path) -> None:
    """Story 3.8's two-phase flow: the web process only ever marks a proposal
    "approved" (DB-only, TradeProposalRepository.mark_approved -- no broker
    access there). clav-core -- which owns the broker -- picks it up and
    submits the order on its *next* cycle via
    DecisionJournal.execute_pending_approvals(), called from ScanCycleService
    itself (not a hand-rolled DecisionJournal in the test)."""
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({"AAPL": _trending_candles("AAPL")}, clock=clock)
    factory = _session_factory(tmp_path)
    policy = ApprovalPolicy(mode="manual", ttl_minutes=30)
    service = _service(factory, data_source, approval_policy=policy, clock=clock)

    cycle_1 = service.run(trigger="manual")
    with session_scope(factory) as session:
        repos = Repositories(session)
        pending = repos.trade_proposals.list_pending()
        assert len(pending) == 1
        proposal_id = pending[0].id

    # The web process's DB-only approve endpoint (no ExecutionEngine/Broker
    # involved at all here).
    with session_scope(factory) as session:
        repos = Repositories(session)
        approved = repos.trade_proposals.mark_approved(
            proposal_id, decided_by="operator", decided_at=clock.now()
        )
        assert approved.status == "approved"

    # clav-core's next scheduled cycle picks it up and submits the order --
    # via ExecutionEngine's deterministic client_order_id, keyed off the
    # ORIGINAL decision's cycle_id (cycle_1), not the cycle that executed it.
    service.run(trigger="manual")
    with session_scope(factory) as session:
        repos = Repositories(session)
        order = repos.orders.get_by_client_order_id(f"clav-{cycle_1}-AAPL-buy")
        assert order is not None

        proposal = repos.trade_proposals.get(proposal_id)
        assert proposal.status == "executed"
        assert proposal.decided_by == "operator"  # attribution preserved from the web approval


def test_manual_mode_hold_never_creates_a_journal_entry(tmp_path) -> None:
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({"AAPL": _flat_candles("AAPL")}, clock=clock)
    factory = _session_factory(tmp_path)
    policy = ApprovalPolicy(mode="manual")
    service = _service(factory, data_source, approval_policy=policy, clock=clock)

    service.run(trigger="manual")

    with session_scope(factory) as session:
        repos = Repositories(session)
        journal = repos.trade_proposals.list_recent(limit=10)
        assert journal == []  # flat technicals -> HOLD -> no proposal at all
