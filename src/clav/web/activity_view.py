"""Plain-language activity feed over the ``decision`` history — the same rows
the operator-facing ``/explanations`` list shows, but rendered as "Bought 10
shares of AAPL — mostly because price is trending up" instead of signed
scores. Used by the home page (top few) and the Activity page (paginated).

One bounded query per page plus one cheap ``order`` lookup per row (to say
"Bought" only when a decision actually executed); nothing here loads more than
the page it renders (Pi RAM discipline).
"""

from __future__ import annotations

from typing import Any

from clav.data import tables
from clav.data.repositories import Repositories
from clav.web import plain_language as pl

_DOT = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}


def _row(repos: Repositories, decision: tables.Decision) -> dict[str, Any]:
    instrument = repos.instruments.get_by_id(decision.instrument_id)
    symbol = instrument.symbol if instrument is not None else ""
    llm = (decision.reasoning or {}).get("llm")

    order = repos.orders.get_by_decision_id(decision.id)
    executed = order is not None
    qty = getattr(order, "qty", None) if order is not None else decision.target_qty

    conf = pl.confidence_label(llm.get("conviction") if llm else None)
    action = (decision.action or "").upper()
    return {
        "id": decision.id,
        "symbol": symbol,
        "action": action,
        "tone": pl.action_tone(action),
        "dot": _DOT.get(action, "⚪"),
        "created_at": decision.created_at,
        "headline": pl.decision_headline(symbol, action, qty, executed=executed),
        "why": pl.plain_reason(decision, llm),
        "confidence": conf,
        "is_llm_driven": llm is not None,
        "is_fallback": bool(llm.get("is_fallback")) if llm else False,
        # Raw numbers kept so operator-facing views can still show them.
        "conviction": llm.get("conviction") if llm else None,
    }


def build_activity_rows(
    repos: Repositories,
    *,
    symbol: str | None = None,
    action: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> list[dict[str, Any]]:
    decisions = repos.decisions.list_recent(
        symbol=symbol, action=action, limit=limit, offset=offset
    )
    return [_row(repos, d) for d in decisions]
