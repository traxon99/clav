"""Story 3.10 — editable strategy prompt / persona store: versioning, atomic
activation, hot-reload (no restart), default seed, and provenance stamping."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from clav.clock import FakeClock
from clav.data.db import make_engine, make_session_factory
from clav.data.tables import Base
from clav.domain.persona import DEFAULT_PERSONA
from clav.integrations.llm import GeminiAnalyst, LLMResult
from clav.services.prompt_store import PromptVersionStore

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def test_no_active_version_falls_back_to_default_persona(session_factory) -> None:
    store = PromptVersionStore(session_factory, clock=FakeClock(NOW))
    content, version_id = store.get_active()
    assert content == DEFAULT_PERSONA
    assert version_id is None


def test_seed_default_creates_active_version_once(session_factory) -> None:
    store = PromptVersionStore(session_factory, clock=FakeClock(NOW))
    first = store.seed_default(persona="custom default")
    assert first.active is True
    assert first.content == "custom default"

    # Idempotent: seeding again does not create a second version or clobber it.
    second = store.seed_default(persona="a different default")
    assert second.id == first.id
    assert second.content == "custom default"
    assert len(store.list_versions()) == 1


def test_edit_creates_new_immutable_version_and_activates_it(session_factory) -> None:
    clock = FakeClock(NOW)
    store = PromptVersionStore(session_factory, clock=clock)
    v1 = store.edit("persona v1", created_by="operator")
    v2 = store.edit("persona v2", created_by="operator")

    assert v1.id != v2.id
    assert v2.active is True

    versions = store.list_versions()
    assert len(versions) == 2
    # previous version retained, now inactive
    v1_row = next(v for v in versions if v.id == v1.id)
    assert v1_row.active is False
    assert v1_row.content == "persona v1"  # history preserved verbatim


def test_activate_switches_atomically_between_existing_versions(session_factory) -> None:
    store = PromptVersionStore(session_factory, clock=FakeClock(NOW))
    v1 = store.edit("v1", created_by="op")
    store.edit("v2", created_by="op")

    reactivated = store.activate(v1.id)
    assert reactivated is not None
    assert reactivated.active is True

    content, version_id = store.get_active()
    assert content == "v1"
    assert version_id == str(v1.id)

    versions = {v.id: v.active for v in store.list_versions()}
    assert sum(versions.values()) == 1  # exactly one active row


def test_analyst_picks_up_edited_prompt_without_restart(session_factory) -> None:
    """The hot-reload requirement: GeminiAnalyst is constructed once with
    store.get_active as its persona_provider; an edit made *after* construction
    is picked up on the very next analyze() call."""
    clock = FakeClock(NOW)
    store = PromptVersionStore(session_factory, clock=clock)
    store.seed_default(persona="original persona")

    captured_prompts: list[str] = []

    class RecordingClient:
        def generate(self, prompt: str) -> LLMResult:
            captured_prompts.append(prompt)
            return LLMResult(text='{"sentiment":0,"conviction":0,"rationale":"x"}', model="m")

    analyst = GeminiAnalyst(RecordingClient(), persona_provider=store.get_active)
    analyst.analyze("AAPL", [], None, {})
    assert "original persona" in captured_prompts[0]

    store.edit("brand new persona after construction", created_by="operator")
    analyst.analyze("AAPL", [], None, {})
    assert "brand new persona after construction" in captured_prompts[1]
    assert "original persona" not in captured_prompts[1]


def test_signal_records_the_prompt_version_id(session_factory) -> None:
    clock = FakeClock(NOW)
    store = PromptVersionStore(session_factory, clock=clock)
    version = store.seed_default(persona="p")

    class Client:
        def generate(self, prompt: str) -> LLMResult:
            return LLMResult(text='{"sentiment":0.2,"conviction":0.3,"rationale":"x"}', model="m")

    analyst = GeminiAnalyst(Client(), persona_provider=store.get_active)
    signal = analyst.analyze("AAPL", [], None, {})
    assert signal.prompt_version == str(version.id)
