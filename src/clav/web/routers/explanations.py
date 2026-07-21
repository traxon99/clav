"""GET /explanations, GET /explanations/{decision_id} (Story 4.6): recent
decisions with the full AI-explanation provenance chain — news/social
inputs, the exact ``analysis_result`` (redacted Gemini request/response),
sentiment/conviction/prompt_version/model, the risk outcome, and the
resulting order/fill/realized P&L. Read-only, paginated, filterable by
symbol/action; no new capture plumbing — Epic 3's ``analysis_result`` and
``decision.reasoning.llm``/``trade_proposal.inputs_ref`` back-links already
store everything this joins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from clav.data import tables
from clav.data.repositories import Repositories
from clav.web.deps import get_repos

router = APIRouter(tags=["explanations"])

_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Bounds a single page regardless of the ?limit= query param -- never load
# the whole decision history into RAM.
MAX_PAGE_LIMIT = 100
DEFAULT_PAGE_LIMIT = 25


def _token(request: Request) -> str | None:
    token: str | None = request.app.state.web_token
    return token


def _row_summary(repos: Repositories, decision: tables.Decision) -> dict[str, Any]:
    instrument = repos.instruments.get_by_id(decision.instrument_id)
    llm = (decision.reasoning or {}).get("llm")
    return {
        "id": decision.id,
        "symbol": instrument.symbol if instrument is not None else "",
        "action": decision.action,
        "created_at": decision.created_at,
        "conviction": llm.get("conviction") if llm else None,
        "is_fallback": bool(llm.get("is_fallback")) if llm else False,
        "is_llm_driven": llm is not None,
    }


@router.get("/explanations", response_class=HTMLResponse)
def explanations_list(
    request: Request,
    symbol: str = "",
    action: str = "",
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    repos: Repositories = Depends(get_repos),
) -> HTMLResponse:
    bounded_limit = max(1, min(limit, MAX_PAGE_LIMIT))
    bounded_offset = max(0, offset)
    symbol_filter = symbol.strip() or None
    action_filter = action.strip().upper() or None

    decisions = repos.decisions.list_recent(
        symbol=symbol_filter, action=action_filter, limit=bounded_limit, offset=bounded_offset
    )
    total = repos.decisions.count_recent(symbol=symbol_filter, action=action_filter)

    return _templates.TemplateResponse(
        request,
        "explanations.html",
        {
            "rows": [_row_summary(repos, d) for d in decisions],
            "symbol": symbol,
            "action": action,
            "limit": bounded_limit,
            "offset": bounded_offset,
            "has_more": bounded_offset + bounded_limit < total,
            "token": _token(request),
        },
    )


@router.get("/explanations/{decision_id}", response_class=HTMLResponse)
def explanation_detail(
    request: Request, decision_id: int, repos: Repositories = Depends(get_repos)
) -> HTMLResponse:
    decision = repos.decisions.get(decision_id)
    if decision is None:
        return _templates.TemplateResponse(
            request,
            "explanation_detail.html",
            {"decision": None, "token": _token(request)},
            status_code=404,
        )

    instrument = repos.instruments.get_by_id(decision.instrument_id)
    llm = (decision.reasoning or {}).get("llm")

    analysis_result = None
    news_items = []
    social_digest = None
    if llm:
        analysis_result_id = llm.get("analysis_result_id")
        if analysis_result_id is not None:
            analysis_result = repos.analysis_results.get(analysis_result_id)
        for news_id in llm.get("news_item_ids") or []:
            item = repos.news_items.get_by_id(news_id)
            if item is not None:
                news_items.append(item)
        social_digest_id = llm.get("social_digest_id")
        if social_digest_id is not None:
            social_digest = repos.social_digests.get_by_id(social_digest_id)

    order = repos.orders.get_by_decision_id(decision_id)
    fills = repos.fills.get_by_order_id(order.id) if order is not None else []
    trade = repos.trades.get_by_entry_decision_id(decision_id)
    proposal = repos.trade_proposals.get_by_decision_id(decision_id)

    return _templates.TemplateResponse(
        request,
        "explanation_detail.html",
        {
            "decision": decision,
            "symbol": instrument.symbol if instrument is not None else "",
            "llm": llm,
            "risk_evaluation": repos.risk_evaluations.get_by_decision_id(decision_id),
            "analysis_result": analysis_result,
            "news_items": news_items,
            "social_digest": social_digest,
            "order": order,
            "fills": fills,
            "trade": trade,
            "proposal": proposal,
            "token": _token(request),
        },
    )
