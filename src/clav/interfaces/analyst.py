"""Analyst — the port that turns news/filings/social into a bounded, structured
signal the decision engine can score (Story 3.4), and that writes a structured
post-mortem for a closed trade (Story 5.2).

``GeminiAnalyst`` (in ``clav.integrations.llm``) is the real implementation. Its
``analyze()`` output is a strictly-validated ``AnalystSignal``: anything
malformed/failed/timed-out degrades to a **neutral** signal (``sentiment=0,
conviction=0``), never an exception that aborts the cycle. The ``llm_signal``
fed to the decision engine is ``sentiment * conviction`` (docs/00-overview.md §4).

``review()`` is deliberately the opposite shape: there is no safe "neutral"
trade review (a fabricated post-mortem would be worse than none), so any
failure raises ``ReviewError`` instead of returning a fallback instance
(epic-05 decision #3). The caller (``TradeReviewService``, Story 5.3/5.4)
decides whether to defer or retry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Literal

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


class ReviewedTrade(BaseModel):
    """The minimal, persistence-free view of a closed trade ``Analyst.review()``
    needs (Story 5.2) -- keeps ``interfaces/`` decoupled from the SQLAlchemy
    ``tables.Trade`` row; ``TradeReviewService`` (Story 5.3) builds one from it."""

    id: int
    symbol: str
    qty: int
    entry_price: float
    exit_price: float
    opened_at: datetime
    closed_at: datetime
    realized_pl: float
    return_pct: float


class ReviewContext(BaseModel):
    """Everything else ``Analyst.review()`` needs to judge one closed trade
    (Story 5.2, docs/07-trade-review.md §2): the entry decision that opened it,
    the risk outcome, the news/social that fed the entry, the price path from
    entry to exit, and the derived exit reason (epic-05 decision #4).
    ``TradeReviewService.build_context()`` (Story 5.3) assembles this from the
    DB's provenance chain; it is a plain Pydantic model here so
    ``GeminiAnalyst.review()`` and its tests never need a database."""

    entry_action: str
    raw_score: float
    technical_score: float
    llm_signal: float
    entry_rationale: dict[str, Any] = Field(default_factory=dict)
    risk_notes: dict[str, Any] = Field(default_factory=dict)
    news_headlines: list[str] = Field(default_factory=list)
    social_summary: str = ""
    price_path: list[float] = Field(default_factory=list)
    exit_reason: str = "unknown"


ConfidenceCalibration = Literal["overconfident", "calibrated", "underconfident"]


class TradeReview(BaseModel):
    """Schema-validated structured post-mortem for one closed trade (Story 5.2,
    docs/07-trade-review.md §3). Unlike ``AnalystSignal`` there is no safe
    "neutral" review -- a response that fails to parse/validate becomes a
    ``ReviewError``, never a fabricated instance of this class."""

    why_entered: str
    supporting_info: list[str] = Field(default_factory=list)
    risks_at_entry: list[str] = Field(default_factory=list)
    reasoning_correct: bool | None = None
    what_worked: list[str] = Field(default_factory=list)
    misleading_signals: list[str] = Field(default_factory=list)
    hindsight_view: str = ""
    improvements: list[str] = Field(default_factory=list)
    confidence_calibration: ConfidenceCalibration
    tags: list[str] = Field(default_factory=list)
    model: str = ""


class ReviewError(Exception):
    """Raised by ``Analyst.review()`` on any failure -- timeout, safety block,
    malformed JSON, or a schema/enum-invalid field. There is no safe neutral
    ``TradeReview`` (epic-05 decision #3); the caller decides whether to defer
    (budget/breaker) or retry-then-terminally-fail (Story 5.4)."""


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

    @abstractmethod
    def review(self, trade: ReviewedTrade, context: ReviewContext) -> TradeReview:
        """Write a structured post-mortem for one closed trade (Story 5.2).

        Must raise ``ReviewError`` on any failure -- timeout, safety block,
        malformed JSON, or a schema/enum-invalid field. There is no safe
        neutral review to fall back to; fabricating one would be worse than
        surfacing the failure (epic-05 decision #3).
        """
