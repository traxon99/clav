"""GET/POST system_control (pause / emergency-stop), mirroring ``clav-ctl``
(Story 3.8). DB-only writes — ``clav-core`` already polls ``system_control``
every cycle (Story 1.10), so this needs no broker access either."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from clav.clock import Clock
from clav.data.repositories import Repositories
from clav.web.deps import get_clock, get_repos, require_token

router = APIRouter(prefix="/api/control", tags=["control"])

EMERGENCY_STOP_KEY = "emergency_stop"
PAUSED_KEY = "paused"


def _status(repos: Repositories) -> dict[str, bool]:
    return {
        "emergency_stop": repos.system_control.get(EMERGENCY_STOP_KEY, "false") == "true",
        "paused": repos.system_control.get(PAUSED_KEY, "false") == "true",
    }


def _set_flag(
    repos: Repositories, clock: Clock, key: str, value: bool, actor: str
) -> dict[str, bool]:
    now = clock.now()
    before = repos.system_control.get(key, "false")
    after = "true" if value else "false"
    repos.system_control.set(key, after, updated_at=now, updated_by=actor)
    repos.audit_log.add(
        ts=now,
        actor=actor,
        action=f"{key}_set",
        entity_type="system_control",
        entity_id=key,
        before={"value": before},
        after={"value": after},
    )
    return _status(repos)


@router.get("")
def get_control_status(repos: Repositories = Depends(get_repos)) -> dict[str, bool]:
    return _status(repos)


@router.post("/estop", dependencies=[Depends(require_token)])
def trip_emergency_stop(
    actor: str = "operator",
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
) -> dict[str, bool]:
    """Trip the emergency stop: vetoes all new BUY entries. Exits still allowed."""
    return _set_flag(repos, clock, EMERGENCY_STOP_KEY, True, actor)


@router.post("/estop/clear", dependencies=[Depends(require_token)])
def clear_emergency_stop(
    actor: str = "operator",
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
) -> dict[str, bool]:
    return _set_flag(repos, clock, EMERGENCY_STOP_KEY, False, actor)


@router.post("/pause", dependencies=[Depends(require_token)])
def pause(
    actor: str = "operator",
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
) -> dict[str, bool]:
    """Pause: vetoes all new BUY entries. Exits still allowed."""
    return _set_flag(repos, clock, PAUSED_KEY, True, actor)


@router.post("/resume", dependencies=[Depends(require_token)])
def resume(
    actor: str = "operator",
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
) -> dict[str, bool]:
    return _set_flag(repos, clock, PAUSED_KEY, False, actor)
