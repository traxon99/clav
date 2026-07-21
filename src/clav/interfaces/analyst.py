"""Analyst — the port that turns news/filings/social into a bounded, structured
signal the decision engine can score (Story 3.4).

``GeminiAnalyst`` (in ``clav.integrations.llm``) is the real implementation. Its
output is a strictly-validated ``AnalystSignal``: anything malformed/failed/
timed-out degrades to a **neutral** signal (``sentiment=0, conviction=0``), never
an exception that aborts the cycle. The ``llm_signal`` fed to the decision engine
is ``sentiment * conviction`` (docs/00-overview.md §4).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field, field_validator

from clav.domain.models import NewsItem, SocialDigest


class AnalystSignal(BaseModel):
    """Schema-validated LLM signal. ``sentiment`` and ``conviction`` are strictly
    range-checked so an out-of-range model response fails validation and is
    replaced by the neutral fallback rather than poisoning the score."""

    sentiment: float = Field(ge=-1.0, le=1.0)
    conviction: float = Field(ge=-1.0, le=1.0)
    catalysts: list[str] = Field(default_factory=list)
    rationale: str = ""
    model: str = ""
    prompt_version: str | None = None
    is_fallback: bool = False

    @field_validator("sentiment", "conviction")
    @classmethod
    def _reject_nan(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
            raise ValueError("sentiment/conviction must be finite")
        return value

    @property
    def llm_signal(self) -> float:
        """Advisory signal for the decision engine: sentiment direction scaled by
        conviction, clamped to [-1, 1] (docs/00-overview.md §4)."""
        return max(-1.0, min(1.0, self.sentiment * self.conviction))

    @classmethod
    def neutral(
        cls, reason: str, *, model: str = "", prompt_version: str | None = None
    ) -> AnalystSignal:
        return cls(
            sentiment=0.0,
            conviction=0.0,
            catalysts=[],
            rationale=f"fallback: {reason}",
            model=model,
            prompt_version=prompt_version,
            is_fallback=True,
        )


class Analyst(ABC):
    @abstractmethod
    def analyze(
        self,
        symbol: str,
        news: list[NewsItem],
        social_digest: SocialDigest | None,
        context: dict[str, Any],
    ) -> AnalystSignal:
        """Turn the compact news/filings/social digest into a bounded signal.

        Must never raise on a bad model response — return ``AnalystSignal.neutral``
        instead so the scan cycle degrades to technical-only.
        """
