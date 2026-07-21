"""GET /portfolio (Story 4.5): equity/drawdown charts (inline SVG), current
exposure/sector allocation, and the open-positions table. Strictly
read-only — adds no write path; queries are bounded so the page never loads
the full snapshot history into RAM (Pi discipline)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from clav.data.repositories import Repositories
from clav.web.charts import sparkline_svg
from clav.web.deps import get_repos

router = APIRouter(tags=["portfolio"])

_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Bounds how much history a single request can pull, regardless of the
# ?limit= query param -- never load the whole portfolio_snapshot table.
MAX_SNAPSHOT_LIMIT = 500
DEFAULT_SNAPSHOT_LIMIT = 200


def _token(request: Request) -> str | None:
    token: str | None = request.app.state.web_token
    return token


def _position_rows(repos: Repositories) -> list[dict[str, Any]]:
    """Open positions with a mark-to-last-known-close unrealized P&L —
    computed from the already-persisted ``candle`` table (clav-web never
    calls the broker), so it reflects the price as of the last successful
    data fetch, not a live quote."""
    rows: list[dict[str, Any]] = []
    for row in repos.positions.get_all():
        if row.qty == 0:
            continue
        instrument = repos.instruments.get_by_id(row.instrument_id)
        symbol = instrument.symbol if instrument is not None else ""
        latest_candles = (
            repos.candles.get_recent(row.instrument_id, "1Day", 1) if instrument else []
        )
        last_close = latest_candles[0].close if latest_candles else None
        unrealized_pl = (
            (last_close - row.avg_entry_price) * row.qty if last_close is not None else None
        )
        rows.append(
            {
                "symbol": symbol,
                "qty": row.qty,
                "avg_entry_price": row.avg_entry_price,
                "last_close": last_close,
                "unrealized_pl": unrealized_pl,
                "stop_price": row.stop_price,
                "take_profit_price": row.take_profit_price,
            }
        )
    return rows


@router.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(
    request: Request,
    limit: int = DEFAULT_SNAPSHOT_LIMIT,
    repos: Repositories = Depends(get_repos),
) -> HTMLResponse:
    bounded_limit = max(1, min(limit, MAX_SNAPSHOT_LIMIT))
    snapshots = repos.portfolio_snapshots.get_recent(limit=bounded_limit)
    latest = snapshots[-1] if snapshots else None

    return _templates.TemplateResponse(
        request,
        "portfolio.html",
        {
            "latest": latest,
            "equity_svg": sparkline_svg([s.equity for s in snapshots], stroke="#2a5db0"),
            "drawdown_svg": sparkline_svg([s.drawdown for s in snapshots], stroke="#b02a2a"),
            "snapshot_count": len(snapshots),
            "positions": _position_rows(repos),
            "token": _token(request),
        },
    )
