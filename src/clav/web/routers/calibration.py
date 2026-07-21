"""GET /calibration (Story 4.9): a descriptive view joining closed trades to
the analysis_result/decision that drove them -- a conviction-vs-realized-P&L
scatter plus a bucketed summary table (mean return / hit-rate by conviction
band, Gemini-driven vs. technical-only). Purely descriptive: reads existing
rows, adds no scored calibration model or review worker (epic decision #6;
the structured retrospective is Epic 5)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from clav.data.repositories import Repositories
from clav.web.calibration import build_calibration_view
from clav.web.deps import get_repos

router = APIRouter(tags=["calibration"])

_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _token(request: Request) -> str | None:
    token: str | None = request.app.state.web_token
    return token


@router.get("/calibration", response_class=HTMLResponse)
def calibration_page(request: Request, repos: Repositories = Depends(get_repos)) -> HTMLResponse:
    view = build_calibration_view(repos)
    return _templates.TemplateResponse(
        request, "calibration.html", {**view, "token": _token(request)}
    )
