"""DecisionJournal — the operator-facing record of every autonomous decision,
plus the optional per-symbol approval gate (Story 3.7).

Default (``approval.mode = auto``): a risk-passing BUY/SELL **executes
autonomously** through ``ExecutionEngine`` and is journaled as ``executed`` (or
``vetoed`` if the risk engine blocked it) with its rationale + input references —
no human, no notification.

Optional (``approval.mode = manual``, off by default, per-symbol override): a
risk-passing **BUY** is written as ``pending`` and does **not** execute until
approved via the control API/UI (Story 3.8/3.9); it expires after
``ttl_minutes`` (fail-closed: expired ⇒ never executes). **SELL decisions
(exits) always auto-execute in either mode** — the entries-vs-exits invariant.
Approve/reject reuse ``ExecutionEngine``'s idempotent ``client_order_id`` path,
so re-approving an already-executed proposal is a no-op, never a second order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from clav.clock import Clock
from clav.common.logging import get_logger
from clav.data.repositories import Repositories
from clav.domain.models import Order, RiskDecision, TradeDecision, TradeProposal
from clav.services.execution import ExecutionEngine

_logger = get_logger(__name__)


@dataclass(frozen=True)
class ApprovalPolicy:
    mode: str = "auto"  # "auto" | "manual"
    ttl_minutes: int = 30
    per_symbol: dict[str, str] | None = None

    def mode_for(self, symbol: str) -> str:
        overrides = self.per_symbol or {}
        return overrides.get(symbol.upper(), self.mode)


@dataclass(frozen=True)
class JournalResult:
    proposal: TradeProposal | None
    order: Order | None


class DecisionJournal:
    def __init__(
        self,
        *,
        repos: Repositories,
        execution: ExecutionEngine,
        clock: Clock,
        policy: ApprovalPolicy,
    ) -> None:
        self._repos = repos
        self._execution = execution
        self._clock = clock
        self._policy = policy

    def expire_stale(self) -> int:
        return self._repos.trade_proposals.expire_stale(self._clock.now())

    def record(
        self,
        *,
        decision: TradeDecision,
        decision_id: int,
        risk_decision: RiskDecision,
        rationale: str = "",
        inputs_ref: dict[str, Any] | None = None,
    ) -> JournalResult:
        if decision.action not in ("BUY", "SELL"):
            return JournalResult(proposal=None, order=None)

        side = "buy" if decision.action == "BUY" else "sell"
        now = self._clock.now()
        inputs_ref = inputs_ref or {}

        if not risk_decision.approved or risk_decision.adjusted_qty <= 0:
            proposal = self._repos.trade_proposals.create(
                decision_id=decision_id,
                symbol=decision.symbol,
                side=side,
                proposed_qty=decision.target_qty,
                rationale=rationale,
                inputs_ref=inputs_ref,
                status="vetoed",
                created_at=now,
                decided_at=now,
                decided_by="system:risk_engine",
            )
            return JournalResult(proposal=proposal, order=None)

        # Only entries (BUY) are ever subject to the approval gate — exits
        # always auto-execute in either mode (entries-vs-exits invariant).
        if side == "buy" and self._policy.mode_for(decision.symbol) == "manual":
            expires_at = now + timedelta(minutes=self._policy.ttl_minutes)
            proposal = self._repos.trade_proposals.create(
                decision_id=decision_id,
                symbol=decision.symbol,
                side=side,
                proposed_qty=risk_decision.adjusted_qty,
                rationale=rationale,
                inputs_ref=inputs_ref,
                status="pending",
                created_at=now,
                expires_at=expires_at,
            )
            _logger.info(
                "decision_journal_pending_approval",
                symbol=decision.symbol,
                proposal_id=proposal.id,
                expires_at=expires_at.isoformat(),
            )
            return JournalResult(proposal=proposal, order=None)

        order = self._execution.execute(decision, risk_decision, decision_id=decision_id)
        status = "executed" if order is not None else "vetoed"
        proposal = self._repos.trade_proposals.create(
            decision_id=decision_id,
            symbol=decision.symbol,
            side=side,
            proposed_qty=risk_decision.adjusted_qty,
            executed_qty=risk_decision.adjusted_qty if order is not None else 0,
            rationale=rationale,
            inputs_ref=inputs_ref,
            status=status,
            created_at=now,
            decided_at=now,
            decided_by="system:auto",
        )
        return JournalResult(proposal=proposal, order=order)

    def approve(self, proposal_id: int, *, decided_by: str) -> JournalResult:
        """Approve a pending BUY: idempotent, reuses ExecutionEngine's
        client_order_id path: re-approving an already-executed proposal
        re-fetches the same order (no second submission) rather than being a
        hard no-op. ``rejected``/``expired`` stay fail-closed — never executed,
        even on a repeated approve() call."""
        self.expire_stale()
        row = self._repos.trade_proposals.get_row(proposal_id)
        if row is None:
            return JournalResult(proposal=None, order=None)
        if row.status in ("rejected", "expired"):
            return JournalResult(proposal=self._repos.trade_proposals.get(proposal_id), order=None)

        decision, risk_decision = self._reconstruct(row.decision_id)
        if decision is None or risk_decision is None:
            _logger.error("decision_journal_approve_missing_decision", proposal_id=proposal_id)
            return JournalResult(proposal=self._repos.trade_proposals.get(proposal_id), order=None)

        was_pending = row.status == "pending"
        order = self._execution.execute(decision, risk_decision, decision_id=row.decision_id)
        if was_pending:
            now = self._clock.now()
            status = "executed" if order is not None else "approved"
            proposal = self._repos.trade_proposals.mark_decided(
                proposal_id,
                status=status,
                decided_at=now,
                decided_by=decided_by,
                executed_qty=risk_decision.adjusted_qty if order is not None else None,
            )
        else:
            # Already decided (executed/approved) — idempotent re-fetch only;
            # don't overwrite who/when it was originally approved.
            proposal = self._repos.trade_proposals.get(proposal_id)
        return JournalResult(proposal=proposal, order=order)

    def reject(self, proposal_id: int, *, decided_by: str) -> TradeProposal | None:
        self.expire_stale()
        row = self._repos.trade_proposals.get_row(proposal_id)
        if row is None or row.status != "pending":
            return self._repos.trade_proposals.get(proposal_id)
        return self._repos.trade_proposals.mark_decided(
            proposal_id, status="rejected", decided_at=self._clock.now(), decided_by=decided_by
        )

    def _reconstruct(
        self, decision_id: int
    ) -> tuple[TradeDecision | None, RiskDecision | None]:
        """Rebuild the TradeDecision/RiskDecision that produced a proposal, so
        ``approve()`` can call the exact same ExecutionEngine.execute() path
        (and therefore the exact same idempotent client_order_id) that the
        auto path would have used."""
        decision_row = self._repos.decisions.get(decision_id)
        if decision_row is None:
            return None, None
        risk_eval_row = self._repos.risk_evaluations.get_by_decision_id(decision_id)
        if risk_eval_row is None:
            return None, None
        decision = TradeDecision(
            cycle_id=decision_row.scan_cycle_id,
            symbol=self._symbol_for(decision_row.instrument_id),
            action=decision_row.action,
            target_qty=decision_row.target_qty,
            raw_score=decision_row.raw_score,
            technical_score=decision_row.technical_score,
            llm_signal=decision_row.llm_signal,
            portfolio_bias=decision_row.portfolio_bias,
            reasoning=decision_row.reasoning,
        )
        risk_decision = RiskDecision(
            approved=risk_eval_row.approved,
            adjusted_qty=risk_eval_row.adjusted_qty,
            blocked_by=risk_eval_row.blocked_by,
            notes=risk_eval_row.notes,
        )
        return decision, risk_decision

    def _symbol_for(self, instrument_id: int) -> str:
        instrument = self._repos.instruments.get_by_id(instrument_id)
        return instrument.symbol if instrument is not None else ""
