"""GET /health — liveness + last-cycle + Gemini breaker/budget state (Story 3.8)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends

from clav.data.repositories import Repositories
from clav.web.deps import get_repos

router = APIRouter(tags=["health"])


@router.get("/health")
def health(repos: Repositories = Depends(get_repos)) -> dict[str, Any]:
    estop = repos.system_control.get("emergency_stop", "false") == "true"
    paused = repos.system_control.get("paused", "false") == "true"
    last_cycle = repos.scan_cycles.latest()

    raw_budget = repos.system_control.get("llm_budget_snapshot")
    llm_budget = json.loads(raw_budget) if raw_budget else None

    return {
        "status": "ok",
        "emergency_stop": estop,
        "paused": paused,
        "last_cycle": (
            {
                "id": last_cycle.id,
                "started_at": last_cycle.started_at.isoformat(),
                "finished_at": (
                    last_cycle.finished_at.isoformat() if last_cycle.finished_at else None
                ),
                "status": last_cycle.status,
                "mode": last_cycle.mode,
                "market_open": last_cycle.market_open,
            }
            if last_cycle is not None
            else None
        ),
        "llm_budget": llm_budget,
    }
