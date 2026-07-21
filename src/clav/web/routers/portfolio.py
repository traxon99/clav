"""GET /portfolio (Story 4.5): equity/drawdown charts (inline SVG), current
exposure/sector allocation, and the open-positions table. Strictly
read-only — adds no write path; queries are bounded so the page never loads
the full snapshot history into RAM (Pi discipline)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from clav.data.repositories import Repositories
from clav.web.charts import sparkline_svg
from clav.web.deps import get_repos
from clav.web.positions_view import build_position_rows

router = APIRouter(tags=["portfolio"])

_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Bounds how much history a single request can pull, regardless of the
# ?limit= query param -- never load the whole portfolio_snapshot table.
MAX_SNAPSHOT_LIMIT = 500
DEFAULT_SNAPSHOT_LIMIT = 200


def _token(request: Request) -> str | None:
    token: str | None = request.app.state.web_token
    return token


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
            "positions": build_position_rows(repos),
            "token": _token(request),
        },
    )
