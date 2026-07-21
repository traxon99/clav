"""Story 3.7 — decision journal & optional approval gate.

auto mode: risk-passing entry executes + journaled `executed`, no wait.
manual mode: BUY holds `pending` until approved; approve => exactly one
order; reject/expiry => none. Exits always execute in either mode.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from clav.clock import FakeClock
from clav.data.db import make_engine, make_session_factory
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import RiskDecision, TradeDecision
from clav.integrations.dryrun_broker import DryRunBroker
from clav.services.decision_journal import ApprovalPolicy, DecisionJournal
from clav.services.execution import ExecutionEngine

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _decision(action="BUY", symbol="AAPL", cycle_id="c1", target_qty=10) -> TradeDecision:
    return TradeDecision(
        cycle_id=cycle_id,
        symbol=symbol,
        action=action,
        target_qty=target_qty,
        raw_score=0.5,
        technical_score=0.5,
        llm_signal=0.0,
        portfolio_bias=0.0,
        reasoning={},
    )


def _risk(approved=True, adjusted_qty=10) -> RiskDecision:
    return RiskDecision(approved=approved, adjusted_qty=adjusted_qty, blocked_by=[], notes={})


def _setup(session_factory, *, policy: ApprovalPolicy, clock: FakeClock):
    session = session_factory()
    repos = Repositories(session)
    broker = DryRunBroker(clock=clock, market_open=True)
    execution = ExecutionEngine(broker, repos, clock=clock)
    journal = DecisionJournal(repos=repos, execution=execution, clock=clock, policy=policy)
    return session, repos, journal


def _persist_decision(repos, decision: TradeDecision, risk: RiskDecision | None = None) -> int:
    """Mirrors what ScanCycleService always does: a decision row is never
    persisted without its risk_evaluation (DecisionJournal.approve() relies on
    reading both back to reconstruct the exact ExecutionEngine.execute() call)."""
    repos.instruments.get_or_create(decision.symbol)
    repos.scan_cycles.create(
        decision.cycle_id, started_at=NOW, mode="dryrun", trigger="manual"
    )
    decision_id = repos.decisions.add(
        scan_cycle_id=decision.cycle_id,
        instrument_id=repos.instruments.get_by_symbol(decision.symbol).id,
        decision=decision,
        created_at=NOW,
    )
    repos.risk_evaluations.add(decision_id, risk or _risk(), evaluated_at=NOW)
    return decision_id


def test_auto_mode_executes_and_journals_with_rationale(session_factory) -> None:
    clock = FakeClock(NOW)
    policy = ApprovalPolicy(mode="auto")
    session, repos, journal = _setup(session_factory, policy=policy, clock=clock)
    decision = _decision()
    risk = _risk()
    decision_id = _persist_decision(repos, decision, risk)

    result = journal.record(
        decision=decision,
        decision_id=decision_id,
        risk_decision=risk,
        rationale="strong catalyst",
        inputs_ref={"news_item_ids": [1, 2]},
    )
    session.commit()

    assert result.order is not None
    assert result.proposal is not None
    assert result.proposal.status == "executed"
    assert result.proposal.rationale == "strong catalyst"
    assert result.proposal.inputs_ref == {"news_item_ids": [1, 2]}
    assert result.proposal.executed_qty == 10
    session.close()


def test_auto_mode_risk_vetoed_journals_vetoed_no_order(session_factory) -> None:
    clock = FakeClock(NOW)
    policy = ApprovalPolicy(mode="auto")
    session, repos, journal = _setup(session_factory, policy=policy, clock=clock)
    decision = _decision()
    risk = _risk(approved=False, adjusted_qty=0)
    decision_id = _persist_decision(repos, decision, risk)

    result = journal.record(decision=decision, decision_id=decision_id, risk_decision=risk)
    session.commit()

    assert result.order is None
    assert result.proposal.status == "vetoed"
    session.close()


def test_manual_mode_buy_holds_pending_no_order(session_factory) -> None:
    clock = FakeClock(NOW)
    policy = ApprovalPolicy(mode="manual", ttl_minutes=30)
    session, repos, journal = _setup(session_factory, policy=policy, clock=clock)
    decision = _decision()
    risk = _risk()
    decision_id = _persist_decision(repos, decision, risk)

    result = journal.record(decision=decision, decision_id=decision_id, risk_decision=risk)
    session.commit()

    assert result.order is None
    assert result.proposal.status == "pending"
    assert result.proposal.expires_at == NOW + timedelta(minutes=30)
    session.close()


def test_manual_mode_approve_produces_exactly_one_order(session_factory) -> None:
    clock = FakeClock(NOW)
    policy = ApprovalPolicy(mode="manual")
    session, repos, journal = _setup(session_factory, policy=policy, clock=clock)
    decision = _decision()
    risk = _risk()
    decision_id = _persist_decision(repos, decision, risk)
    pending = journal.record(
        decision=decision, decision_id=decision_id, risk_decision=risk
    ).proposal
    session.commit()

    approved = journal.approve(pending.id, decided_by="operator")
    session.commit()
    assert approved.order is not None
    assert approved.proposal.status == "executed"
    assert approved.proposal.decided_by == "operator"

    # Re-approving is idempotent: no second order, still executed.
    again = journal.approve(pending.id, decided_by="operator")
    session.commit()
    assert again.order is not None
    assert again.order.client_order_id == approved.order.client_order_id
    session.close()


def test_manual_mode_reject_never_executes(session_factory) -> None:
    clock = FakeClock(NOW)
    policy = ApprovalPolicy(mode="manual")
    session, repos, journal = _setup(session_factory, policy=policy, clock=clock)
    decision = _decision()
    risk = _risk()
    decision_id = _persist_decision(repos, decision, risk)
    pending = journal.record(
        decision=decision, decision_id=decision_id, risk_decision=risk
    ).proposal
    session.commit()

    rejected = journal.reject(pending.id, decided_by="operator")
    session.commit()
    assert rejected.status == "rejected"

    # Approving after reject is a fail-closed no-op.
    result = journal.approve(pending.id, decided_by="operator")
    assert result.order is None
    assert result.proposal.status == "rejected"
    session.close()


def test_manual_mode_expiry_fails_closed_never_executes(session_factory) -> None:
    clock = FakeClock(NOW)
    policy = ApprovalPolicy(mode="manual", ttl_minutes=30)
    session, repos, journal = _setup(session_factory, policy=policy, clock=clock)
    decision = _decision()
    risk = _risk()
    decision_id = _persist_decision(repos, decision, risk)
    pending = journal.record(
        decision=decision, decision_id=decision_id, risk_decision=risk
    ).proposal
    session.commit()

    clock.set(NOW + timedelta(minutes=31))
    result = journal.approve(pending.id, decided_by="operator")
    session.commit()

    assert result.order is None
    assert result.proposal.status == "expired"
    session.close()


def test_sell_always_executes_even_in_manual_mode(session_factory) -> None:
    """Entries-vs-exits invariant: SELL is never gated by approval mode."""
    clock = FakeClock(NOW)
    policy = ApprovalPolicy(mode="manual")
    session, repos, journal = _setup(session_factory, policy=policy, clock=clock)
    decision = _decision(action="SELL", target_qty=5)
    risk = _risk(adjusted_qty=5)
    decision_id = _persist_decision(repos, decision, risk)

    result = journal.record(decision=decision, decision_id=decision_id, risk_decision=risk)
    session.commit()

    assert result.order is not None
    assert result.proposal.status == "executed"
    session.close()


def test_per_symbol_override_manual_for_one_symbol_auto_for_others(session_factory) -> None:
    clock = FakeClock(NOW)
    policy = ApprovalPolicy(mode="auto", per_symbol={"TSLA": "manual"})
    session, repos, journal = _setup(session_factory, policy=policy, clock=clock)

    tsla_decision = _decision(symbol="TSLA", cycle_id="c1")
    tsla_id = _persist_decision(repos, tsla_decision)
    tsla_result = journal.record(
        decision=tsla_decision, decision_id=tsla_id, risk_decision=_risk()
    )

    aapl_decision = _decision(symbol="AAPL", cycle_id="c2")
    aapl_id = _persist_decision(repos, aapl_decision)
    aapl_result = journal.record(
        decision=aapl_decision, decision_id=aapl_id, risk_decision=_risk()
    )
    session.commit()

    assert tsla_result.proposal.status == "pending"
    assert tsla_result.order is None
    assert aapl_result.proposal.status == "executed"
    assert aapl_result.order is not None
    session.close()


def test_hold_action_produces_no_proposal(session_factory) -> None:
    clock = FakeClock(NOW)
    session, repos, journal = _setup(session_factory, policy=ApprovalPolicy(), clock=clock)
    decision = _decision(action="HOLD")
    decision_id = _persist_decision(repos, decision)

    result = journal.record(decision=decision, decision_id=decision_id, risk_decision=_risk())
    assert result.proposal is None
    assert result.order is None
    session.close()
