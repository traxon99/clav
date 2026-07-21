"""GET/POST system_control (pause / emergency-stop), mirroring ``clav-ctl``
(Story 3.8). DB-only writes — ``clav-core`` already polls ``system_control``
every cycle (Story 1.10), so this needs no broker access either."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from clav.clock import Clock
from clav.data.repositories import Repositories
from clav.web.deps import (
    EMERGENCY_STOP_KEY,
    PAUSED_KEY,
    control_status,
    get_clock,
    get_repos,
    require_token,
    set_control_flag,
)

router = APIRouter(prefix="/api/control", tags=["control"])


@router.get("")
def get_control_status(repos: Repositories = Depends(get_repos)) -> dict[str, bool]:
    return control_status(repos)


@router.post("/estop", dependencies=[Depends(require_token)])
def trip_emergency_stop(
    actor: str = "operator",
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
) -> dict[str, bool]:
    """Trip the emergency stop: vetoes all new BUY entries. Exits still allowed."""
    return set_control_flag(repos, clock, EMERGENCY_STOP_KEY, True, actor)


@router.post("/estop/clear", dependencies=[Depends(require_token)])
def clear_emergency_stop(
    actor: str = "operator",
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
) -> dict[str, bool]:
    return set_control_flag(repos, clock, EMERGENCY_STOP_KEY, False, actor)


@router.post("/pause", dependencies=[Depends(require_token)])
def pause(
    actor: str = "operator",
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
) -> dict[str, bool]:
    """Pause: vetoes all new BUY entries. Exits still allowed."""
    return set_control_flag(repos, clock, PAUSED_KEY, True, actor)


@router.post("/resume", dependencies=[Depends(require_token)])
def resume(
    actor: str = "operator",
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
) -> dict[str, bool]:
    return set_control_flag(repos, clock, PAUSED_KEY, False, actor)
