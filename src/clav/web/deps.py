"""Shared FastAPI dependencies (Story 3.8): a per-request DB session/repository
unit-of-work, and the optional shared-token auth for state-changing requests
(epic decision #7 — access method is the gate; the token is off by default,
enforced only when ``cfg.web.token`` is configured, and only on writes)."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Header, HTTPException, Request, status

from clav.clock import Clock
from clav.data.repositories import Repositories

EMERGENCY_STOP_KEY = "emergency_stop"
PAUSED_KEY = "paused"


def get_repos(request: Request) -> Iterator[Repositories]:
    session_factory = request.app.state.session_factory
    session = session_factory()
    try:
        repos = Repositories(session)
        yield repos
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_clock(request: Request) -> Clock:
    clock: Clock = request.app.state.clock
    return clock


def require_token(
    request: Request, x_clav_token: str | None = Header(default=None)
) -> None:
    """Enforced only on state-changing JSON API routes, and only when a token
    is configured. A single-operator LAN-bound deployment needs neither — see
    epic decision #7."""
    token: str | None = request.app.state.web_token
    if token is None:
        return
    if x_clav_token != token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing token"
        )


def control_status(repos: Repositories) -> dict[str, bool]:
    return {
        "emergency_stop": repos.system_control.get(EMERGENCY_STOP_KEY, "false") == "true",
        "paused": repos.system_control.get(PAUSED_KEY, "false") == "true",
    }


def set_control_flag(
    repos: Repositories, clock: Clock, key: str, value: bool, actor: str
) -> dict[str, bool]:
    """Shared by the JSON control routes (Story 3.8) and the HTML control
    forms (Story 3.9) — both are DB-only writes ``clav-core`` polls each
    cycle, mirroring ``clav-ctl``'s own audit-logged toggle."""
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
    return control_status(repos)


def check_ui_token(request: Request, submitted: str | None) -> None:
    """Same optional-token gate as ``require_token``, for the Story 3.9 HTML
    forms — a plain ``<form>`` POST can't set a custom header, so the token
    (when configured) travels as a hidden field instead (see
    ``templates/base.html``'s ``{% if token %}`` block)."""
    token: str | None = request.app.state.web_token
    if token is None:
        return
    if submitted != token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing token"
        )
