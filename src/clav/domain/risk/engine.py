"""RiskEngine — the mandatory safety gate between DecisionEngine and
ExecutionEngine. Takes the minimum cap across all rules and blocks if any rule
vetoes; never creates or enlarges a trade (docs/06-safety-and-risk.md §1-2)."""

from __future__ import annotations

from typing import Any

from clav.domain.models import RiskDecision
from clav.domain.risk.rules import RiskContext, RiskRule


class RiskEngine:
    def __init__(self, rules: list[RiskRule]) -> None:
        self._rules = rules

    def evaluate(self, ctx: RiskContext) -> RiskDecision:
        if ctx.decision.action == "HOLD" or ctx.decision.target_qty <= 0:
            return RiskDecision(
                approved=False,
                adjusted_qty=0,
                blocked_by=[],
                notes={"reason": "no actionable decision"},
            )

        outcomes = [rule.apply(ctx) for rule in self._rules]
        blocked_by = [o.rule_name for o in outcomes if not o.passed]
        caps = [o.max_qty for o in outcomes if o.max_qty is not None]

        adjusted_qty = min([ctx.decision.target_qty, *caps]) if caps else ctx.decision.target_qty
        adjusted_qty = max(adjusted_qty, 0)
        approved = not blocked_by and adjusted_qty > 0
        if not approved:
            adjusted_qty = 0

        notes: dict[str, Any] = {
            o.rule_name: {"passed": o.passed, "max_qty": o.max_qty, "reason": o.reason}
            for o in outcomes
        }
        return RiskDecision(
            approved=approved, adjusted_qty=adjusted_qty, blocked_by=blocked_by, notes=notes
        )
