"""Gemini analyst + LLM client (Story 3.4).

``GeminiAnalyst`` implements the ``Analyst`` port over an ``LLMClient`` (the
default is a keyless-until-configured ``GeminiRestClient`` hitting the Generative
Language REST API). No Google SDK dependency — plain ``httpx`` + JSON, injectable
for tests, so **no live Gemini touches CI**.
"""

from __future__ import annotations

from clav.integrations.llm.analyst import GeminiAnalyst
from clav.integrations.llm.budget import (
    GeminiBudget,
    GuardedLLMClient,
    LLMBreakerOpen,
    LLMBudgetExceeded,
)
from clav.integrations.llm.client import (
    GeminiRestClient,
    LLMBlockedError,
    LLMClient,
    LLMResult,
)

__all__ = [
    "GeminiAnalyst",
    "GeminiBudget",
    "GeminiRestClient",
    "GuardedLLMClient",
    "LLMBlockedError",
    "LLMBreakerOpen",
    "LLMBudgetExceeded",
    "LLMClient",
    "LLMResult",
]
