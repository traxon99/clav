"""GET/PUT the Gemini strategy prompt/persona (Story 3.8, backed by the
Story-3.10 ``PromptVersionStore``). Editing creates a new immutable version and
activates it atomically; ``GeminiAnalyst`` picks it up on its very next call —
no ``clav-core`` restart required."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from clav.domain.models import PromptVersion
from clav.services.prompt_store import PromptVersionStore
from clav.web.deps import require_token

router = APIRouter(prefix="/api/prompt", tags=["prompt"])

# Bounds a single page regardless of the ?limit= query param (Story 4.10's
# RAM-discipline guard) -- never load the whole prompt_version table.
MAX_VERSIONS_LIMIT = 100


def get_prompt_store(request: Request) -> PromptVersionStore:
    store: PromptVersionStore = request.app.state.prompt_store
    return store


@router.get("")
def get_active_prompt(store: PromptVersionStore = Depends(get_prompt_store)) -> dict[str, Any]:
    content, version_id = store.get_active()
    return {"content": content, "version_id": version_id}


@router.get("/versions")
def list_prompt_versions(
    limit: int = 20, store: PromptVersionStore = Depends(get_prompt_store)
) -> list[PromptVersion]:
    return store.list_versions(limit=max(1, min(limit, MAX_VERSIONS_LIMIT)))


@router.put("", dependencies=[Depends(require_token)])
def edit_prompt(
    payload: dict[str, str],
    actor: str = "operator",
    store: PromptVersionStore = Depends(get_prompt_store),
) -> PromptVersion:
    content = payload.get("content", "").strip()
    if not content:
        raise HTTPException(status_code=422, detail="content must not be empty")
    return store.edit(content, created_by=actor)


@router.post("/versions/{version_id}/activate", dependencies=[Depends(require_token)])
def activate_prompt_version(
    version_id: int, store: PromptVersionStore = Depends(get_prompt_store)
) -> PromptVersion:
    version = store.activate(version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="prompt version not found")
    return version
