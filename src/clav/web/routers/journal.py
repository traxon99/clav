"""Decision journal routes (Story 3.7/3.8): browse every autonomous decision
with its full rationale, and — only meaningful when the optional approval
mode is on for a symbol — approve/reject a `pending` entry.

Approve/reject here are **DB-only** (``TradeProposalRepository.mark_approved``/
``mark_rejected``): this process never touches the broker (epic decision #7,
"never exposes brokerage keys"). ``clav-core``'s ``DecisionJournal.
execute_pending_approvals()`` performs the actual submission on its next
cycle — the same poll-the-DB pattern already used for emergency_stop/paused.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from clav.clock import Clock
from clav.data.repositories import Repositories
from clav.domain.models import TradeProposal
from clav.web.deps import get_clock, get_repos, require_token

router = APIRouter(prefix="/api/journal", tags=["journal"])

# Bounds a single page regardless of the ?limit= query param (Story 4.10's
# RAM-discipline guard) -- never load the whole trade_proposal table.
MAX_JOURNAL_LIMIT = 200


@router.get("")
def list_journal(limit: int = 50, repos: Repositories = Depends(get_repos)) -> list[TradeProposal]:
    return repos.trade_proposals.list_recent(limit=max(1, min(limit, MAX_JOURNAL_LIMIT)))


@router.get("/{proposal_id}")
def get_journal_entry(proposal_id: int, repos: Repositories = Depends(get_repos)) -> dict[str, Any]:
    proposal = repos.trade_proposals.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="trade proposal not found")

    decision_row = repos.decisions.get(proposal.decision_id)
    risk_eval_row = repos.risk_evaluations.get_by_decision_id(proposal.decision_id)

    return {
        "proposal": proposal.model_dump(mode="json"),
        "decision": (
            {
                "id": decision_row.id,
                "action": decision_row.action,
                "raw_score": decision_row.raw_score,
                "technical_score": decision_row.technical_score,
                "llm_signal": decision_row.llm_signal,
                "portfolio_bias": decision_row.portfolio_bias,
                "reasoning": decision_row.reasoning,
                "created_at": decision_row.created_at.isoformat(),
            }
            if decision_row is not None
            else None
        ),
        "risk_evaluation": (
            {
                "approved": risk_eval_row.approved,
                "adjusted_qty": risk_eval_row.adjusted_qty,
                "blocked_by": risk_eval_row.blocked_by,
                "notes": risk_eval_row.notes,
            }
            if risk_eval_row is not None
            else None
        ),
    }


@router.post("/{proposal_id}/approve", dependencies=[Depends(require_token)])
def approve_journal_entry(
    proposal_id: int,
    actor: str = "operator",
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
) -> TradeProposal:
    proposal = repos.trade_proposals.mark_approved(
        proposal_id, decided_by=actor, decided_at=clock.now()
    )
    if proposal is None:
        raise HTTPException(status_code=404, detail="trade proposal not found")
    return proposal


@router.post("/{proposal_id}/reject", dependencies=[Depends(require_token)])
def reject_journal_entry(
    proposal_id: int,
    actor: str = "operator",
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
) -> TradeProposal:
    proposal = repos.trade_proposals.mark_rejected(
        proposal_id, decided_by=actor, decided_at=clock.now()
    )
    if proposal is None:
        raise HTTPException(status_code=404, detail="trade proposal not found")
    return proposal
