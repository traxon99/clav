"""Analysis-provenance capture (Story 3.12 closure) and review-provenance
capture (Story 5.4).

Only ``GeminiAnalyst`` sees the prompt it sent and the raw text Gemini returned;
only ``AnalystGateway``/``TradeReviewService`` hold the per-cycle/per-pass DB
session. ``AnalysisCapture``/``ReviewCapture`` bridge the two: each is installed
as the analyst's ``provenance_sink``/``review_provenance_sink`` at composition
time, buffers the **last** call's redacted request/response, and the caller
drains it (``take()``) immediately after each ``analyze()``/``review()`` to
persist an ``analysis_result``/``trade_review`` row with the current session.

Single-cycle (or single-pass), single-thread by construction — CLAV runs one
APScheduler job at a time and processes symbols/trades sequentially (docs/03),
so "last call" is always the call the caller is about to persist. ``take()``
clears the buffer so a symbol/trade that produced no Gemini response (client
error/timeout/budget-open, where the sink never fires) can't accidentally
inherit a previous one's record.
"""

from __future__ import annotations

from dataclasses import dataclass

from clav.integrations.llm.client import LLMResult
from clav.interfaces.analyst import AnalystSignal, TradeReview


@dataclass(frozen=True)
class AnalysisRecord:
    symbol: str
    request: str
    response: str
    model: str
    prompt_version: str | None
    sentiment: float
    conviction: float
    is_fallback: bool
    prompt_tokens: int
    completion_tokens: int


class AnalysisCapture:
    def __init__(self) -> None:
        self._last: AnalysisRecord | None = None

    def record(
        self,
        symbol: str,
        prompt: str,
        response_text: str,
        signal: AnalystSignal,
        usage: LLMResult,
    ) -> None:
        """Matches the ``ProvenanceSink`` signature (installed on GeminiAnalyst)."""
        self._last = AnalysisRecord(
            symbol=symbol,
            request=prompt,
            response=response_text,
            model=usage.model or signal.model,
            prompt_version=signal.prompt_version,
            sentiment=signal.sentiment,
            conviction=signal.conviction,
            is_fallback=signal.is_fallback,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
        )

    def take(self) -> AnalysisRecord | None:
        record, self._last = self._last, None
        return record


@dataclass(frozen=True)
class ReviewRecord:
    trade_id: int
    request: str
    response: str
    model: str
    prompt_tokens: int
    completion_tokens: int


class ReviewCapture:
    """Story 5.4's analogue of ``AnalysisCapture``: drained by
    ``TradeReviewService`` right after a successful ``Analyst.review()`` call
    to populate ``trade_review.raw_response``. Never populated on a failed
    call — ``GeminiAnalyst.review()`` only invokes the sink after a review
    parses and validates (epic-05 decision #3: a failure is logged, never
    persisted)."""

    def __init__(self) -> None:
        self._last: ReviewRecord | None = None

    def record(
        self,
        trade_id: int,
        prompt: str,
        response_text: str,
        review: TradeReview,
        usage: LLMResult,
    ) -> None:
        """Matches the ``ReviewProvenanceSink`` signature (installed on GeminiAnalyst)."""
        self._last = ReviewRecord(
            trade_id=trade_id,
            request=prompt,
            response=response_text,
            model=usage.model or review.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
        )

    def take(self) -> ReviewRecord | None:
        record, self._last = self._last, None
        return record
