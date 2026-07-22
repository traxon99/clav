"""GET /reviews, GET /reviews/{trade_id} (Story 5.5): the Epic-5 trade-review
journal. The list shows every closed trade -- not just the ones that already
have a `trade_review` row -- so a still-`pending` or terminally-`failed`
review renders clearly instead of silently vanishing from the list (Story
5.4's TradeReviewService owns writing those rows; this router only reads
them). The detail page renders a trade's full review history (newest first,
epic-05 decision #6 -- a manual rerun appends rather than overwrites) plus a
link to its entry's ``/explanations/{decision_id}`` for the rationale side --
no duplicate rendering of what Epic 4 already shows there.

Tag filtering has no SQL-level JSON-containment query (Story 5.1 decision);
symbol/tag/calibration filters are all applied in Python over the bounded
``TradeReviewRepository.MAX_RECENT`` most-recently-closed trades, then
paginated in-memory -- correct pagination, at the (Pi-appropriate) cost of
only ever considering that many trades when a filter is active.
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

router = APIRouter(tags=["reviews"])

_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Bounds a single page regardless of the ?limit= query param -- never load
# the whole trade table into RAM.
MAX_PAGE_LIMIT = 100
DEFAULT_PAGE_LIMIT = 25


def _token(request: Request) -> str | None:
    token: str | None = request.app.state.web_token
    return token


def _latest_review(repos: Repositories, trade_id: int) -> tables.TradeReviewRow | None:
    history = repos.trade_reviews.list_for_trade(trade_id)  # newest first
    return history[0] if history else None


def _row_summary(repos: Repositories, trade: tables.Trade) -> dict[str, Any]:
    instrument = repos.instruments.get_by_id(trade.instrument_id)
    latest = _latest_review(repos, trade.id)
    return {
        "trade_id": trade.id,
        "symbol": instrument.symbol if instrument is not None else "",
        "closed_at": trade.closed_at,
        "review_status": trade.review_status,
        "review_attempts": trade.review_attempts,
        "confidence_calibration": latest.confidence_calibration if latest is not None else None,
        "tags": latest.tags if latest is not None else [],
    }


@router.get("/reviews", response_class=HTMLResponse)
def reviews_list(
    request: Request,
    symbol: str = "",
    tag: str = "",
    calibration: str = "",
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    repos: Repositories = Depends(get_repos),
) -> HTMLResponse:
    bounded_limit = max(1, min(limit, MAX_PAGE_LIMIT))
    bounded_offset = max(0, offset)
    symbol_filter = symbol.strip().upper() or None
    tag_filter = tag.strip() or None
    calibration_filter = calibration.strip() or None

    trades = repos.trades.list_closed(limit=repos.trade_reviews.MAX_RECENT)
    rows = [_row_summary(repos, trade) for trade in trades]

    if symbol_filter is not None:
        rows = [r for r in rows if r["symbol"] == symbol_filter]
    if calibration_filter is not None:
        rows = [r for r in rows if r["confidence_calibration"] == calibration_filter]
    if tag_filter is not None:
        rows = [r for r in rows if tag_filter in (r["tags"] or [])]

    total = len(rows)
    page = rows[bounded_offset : bounded_offset + bounded_limit]

    return _templates.TemplateResponse(
        request,
        "reviews.html",
        {
            "rows": page,
            "symbol": symbol,
            "tag": tag,
            "calibration": calibration,
            "limit": bounded_limit,
            "offset": bounded_offset,
            "has_more": bounded_offset + bounded_limit < total,
            "token": _token(request),
        },
    )


@router.get("/reviews/{trade_id}", response_class=HTMLResponse)
def review_detail(
    request: Request, trade_id: int, repos: Repositories = Depends(get_repos)
) -> HTMLResponse:
    trade = repos.trades.get(trade_id)
    if trade is None:
        return _templates.TemplateResponse(
            request,
            "review_detail.html",
            {"trade": None, "token": _token(request)},
            status_code=404,
        )

    instrument = repos.instruments.get_by_id(trade.instrument_id)
    history = repos.trade_reviews.list_for_trade(trade_id)  # newest first

    return _templates.TemplateResponse(
        request,
        "review_detail.html",
        {
            "trade": trade,
            "symbol": instrument.symbol if instrument is not None else "",
            "history": history,
            "token": _token(request),
        },
    )
