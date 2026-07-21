"""PromptVersionStore — the hot-reloaded persona/prompt provider for
``GeminiAnalyst`` (Story 3.10).

Opens its own short-lived DB session per call (matching the one-session-per-
unit-of-work pattern used elsewhere), so editing the prompt via the control API
(Story 3.8) takes effect on the **very next** analyst call — no process restart,
because ``get_active`` (used as ``GeminiAnalyst``'s ``persona_provider``) always
re-reads the current active row.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from clav.clock import Clock
from clav.data.db import session_scope
from clav.data.repositories import Repositories
from clav.domain.models import PromptVersion
from clav.domain.persona import DEFAULT_PERSONA


class PromptVersionStore:
    def __init__(self, session_factory: sessionmaker[Session], *, clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def seed_default(
        self, *, persona: str = DEFAULT_PERSONA, created_by: str = "system"
    ) -> PromptVersion:
        """Idempotent startup seed — a fresh install always has a working
        prompt; an operator's prior edit is never overwritten."""
        with session_scope(self._session_factory) as session:
            repos = Repositories(session)
            return repos.prompt_versions.seed_default_if_missing(
                content=persona, created_by=created_by, created_at=self._clock.now()
            )

    def get_active(self) -> tuple[str, str | None]:
        """``GeminiAnalyst``'s ``PersonaProvider`` signature: (content, version_id)."""
        with session_scope(self._session_factory) as session:
            repos = Repositories(session)
            active = repos.prompt_versions.get_active()
            if active is None:
                return DEFAULT_PERSONA, None
            return active.content, str(active.id)

    def edit(self, content: str, *, created_by: str) -> PromptVersion:
        """The "edit the prompt" flow: create + atomically activate a new
        immutable version. The previous version is retained (history)."""
        with session_scope(self._session_factory) as session:
            repos = Repositories(session)
            return repos.prompt_versions.create_and_activate(
                content=content, created_by=created_by, created_at=self._clock.now()
            )

    def activate(self, version_id: int) -> PromptVersion | None:
        with session_scope(self._session_factory) as session:
            repos = Repositories(session)
            return repos.prompt_versions.activate(version_id)

    def list_versions(self, *, limit: int = 20) -> list[PromptVersion]:
        with session_scope(self._session_factory) as session:
            repos = Repositories(session)
            return repos.prompt_versions.list_versions(limit=limit)
