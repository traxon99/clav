"""GET/PUT effective config subset — weights, risk knobs, watchlist, schedule
(Story 3.8). Writes validate exactly like boot-time config (the very same
Pydantic models — ``WeightsConfig``/``ThresholdsConfig``/``RiskKnobsOverride``/
``RuntimeOverrides``), so a write can never relax a value past what
``config.yaml`` itself would reject. Persisted via ``RuntimeConfigStore``;
``ScanCycleService`` re-reads and live-applies it on the very next cycle."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from clav.clock import Clock
from clav.config import RuntimeOverrides
from clav.data.repositories import Repositories
from clav.services.runtime_config import RuntimeConfigStore
from clav.web.deps import get_clock, get_repos, require_token

router = APIRouter(prefix="/api/config", tags=["config"])


def get_runtime_config(request: Request) -> RuntimeConfigStore:
    store: RuntimeConfigStore = request.app.state.runtime_config
    return store


@router.get("")
def get_config(
    repos: Repositories = Depends(get_repos),
    store: RuntimeConfigStore = Depends(get_runtime_config),
) -> dict[str, Any]:
    """The current operator override (unset fields fall back to boot config
    at the next scan cycle — this endpoint returns the override as stored,
    not a merged view, so the operator can see exactly what's been changed)."""
    return store.get(repos).model_dump(mode="json")


@router.put("", dependencies=[Depends(require_token)])
def put_config(
    payload: dict[str, Any],
    actor: str = "operator",
    repos: Repositories = Depends(get_repos),
    store: RuntimeConfigStore = Depends(get_runtime_config),
    clock: Clock = Depends(get_clock),
) -> dict[str, Any]:
    try:
        overrides = RuntimeOverrides.model_validate(payload)
    except ValidationError as exc:
        # str(exc), not exc.errors() -- Pydantic v2 embeds the raw exception
        # object in a custom validator's error `ctx`, which isn't JSON
        # serializable and would 500 the error response itself.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    store.set(repos, overrides, now=clock.now(), updated_by=actor)
    return overrides.model_dump(mode="json")
