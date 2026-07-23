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

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from clav.clock import Clock
from clav.config import (
    RiskKnobsOverride,
    RuntimeLLMOverride,
    RuntimeOverrides,
    Settings,
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
from clav.web.health_view import build_health_view
from clav.web.portfolio_value import build_portfolio_value_view
from clav.web.positions_view import build_position_rows

router = APIRouter(tags=["ui"])

_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Bounds the dashboard's journal listing regardless of the ?limit= query
# param (Story 4.10's RAM-discipline guard).
MAX_JOURNAL_LIMIT = 200


def _token(request: Request) -> str | None:
    token: str | None = request.app.state.web_token
    return token


def _settings(request: Request) -> Settings:
    cfg: Settings = request.app.state.cfg
    return cfg


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
    request: Request,
    limit: int = 30,
    period: str = "1d",
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
    cfg: Settings = Depends(_settings),
) -> HTMLResponse:
    journal = repos.trade_proposals.list_recent(limit=max(1, min(limit, MAX_JOURNAL_LIMIT)))
    snapshot = repos.portfolio_snapshots.latest()

    return _templates.TemplateResponse(
        request,
        "index.html",
        {
            "journal": journal,
            "snapshot": snapshot,
            "positions": build_position_rows(repos),
            "health": _health(repos),
            "health_tiles": build_health_view(
                repos, clock.now(), scan_interval_minutes=cfg.scan_interval_minutes
            ),
            "portfolio_value": build_portfolio_value_view(repos, clock.now(), period),
            "token": _token(request),
        },
    )


@router.get("/partials/health-tiles", response_class=HTMLResponse)
def health_tiles_partial(
    request: Request,
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
    cfg: Settings = Depends(_settings),
) -> HTMLResponse:
    """Story 4.8 — the HTMX polling target that refreshes the health-tile
    header in place, without a full-page reload."""
    return _templates.TemplateResponse(
        request,
        "_health_tiles.html",
        {
            "health_tiles": build_health_view(
                repos, clock.now(), scan_interval_minutes=cfg.scan_interval_minutes
            )
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


@router.post("/reviews/{trade_id}/rerun")
def rerun_review_via_ui(
    request: Request,
    trade_id: int,
    token_field: str | None = Form(default=None, alias="_token"),
    repos: Repositories = Depends(get_repos),
) -> RedirectResponse:
    """Story 5.7's HTML-form counterpart to ``POST /api/reviews/{trade_id}/
    rerun`` (``reviews.py``) -- a plain form can't set the API route's
    header token, so it travels as a hidden field instead, same as every
    other state-changing form in this UI (``check_ui_token``)."""
    check_ui_token(request, token_field)
    repos.trades.reset_for_rerun(trade_id)
    return RedirectResponse(url=f"/reviews/{trade_id}", status_code=303)


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


class _LLMPreset:
    def __init__(
        self, *, label: str, description: str, model: str, thinking_budget: int, interval: int
    ) -> None:
        self.label = label
        self.description = description
        self.llm = RuntimeLLMOverride(model=model, thinking_budget=thinking_budget)
        self.interval = interval


# The analysis-effort toggle: two named presets bundling a Gemini
# model/thinking-budget pair with a scan cadence, so switching "how hard
# Gemini thinks" and "how often we scan" is one click instead of five fields.
# Applied live (services/scan_cycle.py + services/scheduler.py) -- no
# clav-core restart needed, even for the model swap.
LLM_PRESETS: dict[str, _LLMPreset] = {
    "fast": _LLMPreset(
        label="Fast",
        description=(
            "gemini-3.1-flash-lite, no reasoning (thinking_budget=0), scans every 10 minutes. "
            "Cheapest and quickest -- best for a tight feedback loop or a low-signal watchlist."
        ),
        model="gemini-3.1-flash-lite",
        thinking_budget=0,
        interval=10,
    ),
    "thoughtful": _LLMPreset(
        label="Thoughtful",
        description=(
            "gemini-3.5-flash with a bounded reasoning budget (thinking_budget=512), scans "
            "every 30 minutes. Slower and pricier per call, but better at weighing conflicting "
            "signals and skepticism toward hype/manipulation (see the analyst persona prompt)."
        ),
        model="gemini-3.5-flash",
        thinking_budget=512,
        interval=30,
    ),
}


def _active_llm_preset(override: RuntimeOverrides) -> str | None:
    for name, preset in LLM_PRESETS.items():
        if override.llm == preset.llm and override.scan_interval_minutes == preset.interval:
            return name
    return None


@router.get("/config", response_class=HTMLResponse)
def config_page(request: Request, repos: Repositories = Depends(get_repos)) -> HTMLResponse:
    store: RuntimeConfigStore = request.app.state.runtime_config
    override = store.get(repos)
    cfg = _settings(request)
    return _templates.TemplateResponse(
        request,
        "config.html",
        {
            "override": override,
            "token": _token(request),
            "presets": LLM_PRESETS,
            "active_preset": _active_llm_preset(override),
            "boot_llm_model": cfg.llm.model,
            "boot_thinking_budget": cfg.llm.thinking_budget,
            "boot_scan_interval_minutes": cfg.scan_interval_minutes,
        },
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
    current = store.get(repos)
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
            # This form doesn't surface these two -- preserve whatever the
            # analysis-effort preset (or the JSON API) last set, rather than
            # silently wiping it back to "no override" on every save.
            scan_interval_minutes=current.scan_interval_minutes,
            llm=current.llm,
        )
    except ValueError as exc:
        return _templates.TemplateResponse(
            request,
            "config.html",
            {
                "override": current,
                "token": _token(request),
                "presets": LLM_PRESETS,
                "active_preset": _active_llm_preset(current),
                "boot_llm_model": _settings(request).llm.model,
                "boot_thinking_budget": _settings(request).llm.thinking_budget,
                "boot_scan_interval_minutes": _settings(request).scan_interval_minutes,
                "error": str(exc),
            },
            status_code=422,
        )
    store.set(repos, overrides, now=clock.now(), updated_by=actor)
    return RedirectResponse(url="/config", status_code=303)


@router.post("/config/preset", response_model=None)
def apply_llm_preset_via_ui(
    request: Request,
    preset: str = Form(...),
    actor: str = Form("operator"),
    token_field: str | None = Form(default=None, alias="_token"),
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
) -> RedirectResponse:
    """The Fast/Thoughtful buttons: touches only ``llm`` + ``scan_interval_minutes``
    on top of whatever weights/risk/watchlist override is already stored (a
    merge, not a replace -- see ``edit_config_via_ui``'s matching preserve)."""
    check_ui_token(request, token_field)
    chosen = LLM_PRESETS.get(preset)
    if chosen is None:
        raise HTTPException(status_code=422, detail=f"unknown preset: {preset!r}")
    store: RuntimeConfigStore = request.app.state.runtime_config
    current = store.get(repos)
    overrides = current.model_copy(
        update={"llm": chosen.llm, "scan_interval_minutes": chosen.interval}
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
