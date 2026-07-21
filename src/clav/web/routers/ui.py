"""Story 3.9 — the minimal HTMX/server-rendered supervisory UI over the
Story-3.8 API: a decision-journal browser (list + why-drilldown), tuning
controls (prompt/weights/risk-knobs/watchlist), a read-only positions/P&L +
health summary, and a confirmed e-stop/pause. No SPA build step; plain HTML
forms work with JavaScript off, ``hx-boost`` is a progressive enhancement
only. Same access model as 3.8 — private-network-bound, optional token (see
``deps.check_ui_token``). Interactive charts/metrics/log browser are Epic 4.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from clav.clock import Clock
from clav.config import (
    RiskKnobsOverride,
    RuntimeOverrides,
    ThresholdsConfig,
    WeightsConfig,
)
from clav.data.repositories import Repositories
from clav.services.prompt_store import PromptVersionStore
from clav.services.runtime_config import RuntimeConfigStore
from clav.web.deps import (
    EMERGENCY_STOP_KEY,
    PAUSED_KEY,
    check_ui_token,
    control_status,
    get_clock,
    get_repos,
    set_control_flag,
)

router = APIRouter(tags=["ui"])

_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _token(request: Request) -> str | None:
    token: str | None = request.app.state.web_token
    return token


def _health(repos: Repositories) -> dict[str, Any]:
    status = control_status(repos)
    last_cycle = repos.scan_cycles.latest()
    raw_budget = repos.system_control.get("llm_budget_snapshot")
    return {
        "estop": status["emergency_stop"],
        "paused": status["paused"],
        "last_cycle": last_cycle,
        "llm_budget": json.loads(raw_budget) if raw_budget else None,
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request, limit: int = 30, repos: Repositories = Depends(get_repos)
) -> HTMLResponse:
    journal = repos.trade_proposals.list_recent(limit=limit)
    snapshot = repos.portfolio_snapshots.latest()
    positions = [p for p in repos.positions.get_all() if p.qty != 0]
    position_rows = []
    for row in positions:
        instrument = repos.instruments.get_by_id(row.instrument_id)
        position_rows.append(
            {"symbol": instrument.symbol if instrument else "", "qty": row.qty}
        )

    return _templates.TemplateResponse(
        request,
        "index.html",
        {
            "journal": journal,
            "snapshot": snapshot,
            "positions": position_rows,
            "health": _health(repos),
            "token": _token(request),
        },
    )


@router.get("/journal/{proposal_id}", response_class=HTMLResponse)
def journal_detail(
    request: Request, proposal_id: int, repos: Repositories = Depends(get_repos)
) -> HTMLResponse:
    proposal = repos.trade_proposals.get(proposal_id)
    decision_row = repos.decisions.get(proposal.decision_id) if proposal else None
    risk_eval_row = (
        repos.risk_evaluations.get_by_decision_id(proposal.decision_id) if proposal else None
    )
    return _templates.TemplateResponse(
        request,
        "journal_detail.html",
        {
            "proposal": proposal,
            "decision": decision_row,
            "risk_evaluation": risk_eval_row,
            "token": _token(request),
        },
    )


@router.post("/journal/{proposal_id}/approve")
def approve_via_ui(
    request: Request,
    proposal_id: int,
    actor: str = Form("operator"),
    token_field: str | None = Form(default=None, alias="_token"),
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
) -> RedirectResponse:
    check_ui_token(request, token_field)
    repos.trade_proposals.mark_approved(proposal_id, decided_by=actor, decided_at=clock.now())
    return RedirectResponse(url=f"/journal/{proposal_id}", status_code=303)


@router.post("/journal/{proposal_id}/reject")
def reject_via_ui(
    request: Request,
    proposal_id: int,
    actor: str = Form("operator"),
    token_field: str | None = Form(default=None, alias="_token"),
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
) -> RedirectResponse:
    check_ui_token(request, token_field)
    repos.trade_proposals.mark_rejected(proposal_id, decided_by=actor, decided_at=clock.now())
    return RedirectResponse(url=f"/journal/{proposal_id}", status_code=303)


@router.get("/prompt", response_class=HTMLResponse)
def prompt_page(request: Request) -> HTMLResponse:
    store: PromptVersionStore = request.app.state.prompt_store
    content, version_id = store.get_active()
    versions = store.list_versions(limit=10)
    return _templates.TemplateResponse(
        request,
        "prompt.html",
        {
            "content": content,
            "version_id": version_id,
            "versions": versions,
            "token": _token(request),
        },
    )


@router.post("/prompt")
def edit_prompt_via_ui(
    request: Request,
    content: str = Form(...),
    actor: str = Form("operator"),
    token_field: str | None = Form(default=None, alias="_token"),
) -> RedirectResponse:
    check_ui_token(request, token_field)
    store: PromptVersionStore = request.app.state.prompt_store
    stripped = content.strip()
    if stripped:
        store.edit(stripped, created_by=actor)
    return RedirectResponse(url="/prompt", status_code=303)


@router.get("/config", response_class=HTMLResponse)
def config_page(request: Request, repos: Repositories = Depends(get_repos)) -> HTMLResponse:
    store: RuntimeConfigStore = request.app.state.runtime_config
    override = store.get(repos)
    return _templates.TemplateResponse(
        request, "config.html", {"override": override, "token": _token(request)}
    )


@router.post("/config", response_model=None)
def edit_config_via_ui(
    request: Request,
    technical: float = Form(...),
    llm: float = Form(...),
    portfolio: float = Form(...),
    buy_threshold: float = Form(...),
    sell_threshold: float = Form(...),
    max_position_value: float = Form(...),
    max_daily_loss_pct: float = Form(...),
    max_drawdown_pct: float = Form(...),
    max_portfolio_exposure_pct: float = Form(...),
    max_sector_allocation_pct: float = Form(...),
    cooldown_minutes: int = Form(...),
    post_loss_cooldown_minutes: int = Form(...),
    watchlist: str = Form(...),
    actor: str = Form("operator"),
    token_field: str | None = Form(default=None, alias="_token"),
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
) -> HTMLResponse | RedirectResponse:
    check_ui_token(request, token_field)
    store: RuntimeConfigStore = request.app.state.runtime_config
    symbols = [s.strip() for s in watchlist.split(",") if s.strip()]
    try:
        overrides = RuntimeOverrides(
            weights=WeightsConfig(technical=technical, llm=llm, portfolio=portfolio),
            thresholds=ThresholdsConfig(buy=buy_threshold, sell=sell_threshold),
            risk=RiskKnobsOverride(
                max_position_value=max_position_value,
                max_daily_loss_pct=max_daily_loss_pct,
                max_drawdown_pct=max_drawdown_pct,
                max_portfolio_exposure_pct=max_portfolio_exposure_pct,
                max_sector_allocation_pct=max_sector_allocation_pct,
                cooldown_minutes=cooldown_minutes,
                post_loss_cooldown_minutes=post_loss_cooldown_minutes,
            ),
            watchlist=symbols,
        )
    except ValueError as exc:
        return _templates.TemplateResponse(
            request,
            "config.html",
            {"override": store.get(repos), "token": _token(request), "error": str(exc)},
            status_code=422,
        )
    store.set(repos, overrides, now=clock.now(), updated_by=actor)
    return RedirectResponse(url="/config", status_code=303)


@router.post("/control/{action}")
def control_action_via_ui(
    request: Request,
    action: str,
    actor: str = Form("operator"),
    token_field: str | None = Form(default=None, alias="_token"),
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
) -> RedirectResponse:
    check_ui_token(request, token_field)
    mapping = {
        "estop": (EMERGENCY_STOP_KEY, True),
        "estop-clear": (EMERGENCY_STOP_KEY, False),
        "pause": (PAUSED_KEY, True),
        "resume": (PAUSED_KEY, False),
    }
    if action in mapping:
        key, value = mapping[action]
        set_control_flag(repos, clock, key, value, actor)
    return RedirectResponse(url="/", status_code=303)
