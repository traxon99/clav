"""Shared FastAPI dependencies (Story 3.8): a per-request DB session/repository
unit-of-work, and the optional shared-token auth for state-changing requests
(epic decision #7 — access method is the gate; the token is off by default,
enforced only when ``cfg.web.token`` is configured, and only on writes)."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Header, HTTPException, Request, status

from clav.clock import Clock
from clav.data.repositories import Repositories


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
    """Enforced only on state-changing routes, and only when a token is
    configured. A single-operator LAN-bound deployment needs neither — see
    epic decision #7."""
    token: str | None = request.app.state.web_token
    if token is None:
        return
    if x_clav_token != token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing token"
        )
