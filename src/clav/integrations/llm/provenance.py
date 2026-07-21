"""Analysis-provenance capture (Story 3.12 closure).

Only ``GeminiAnalyst`` sees the prompt it sent and the raw text Gemini returned;
only ``AnalystGateway`` holds the per-cycle DB session. ``AnalysisCapture``
bridges the two: it is installed as the analyst's ``provenance_sink`` at
composition time, buffers the **last** call's redacted request/response, and the
gateway drains it (``take()``) immediately after each ``analyze()`` to persist an
``analysis_result`` row with the current session.

Single-cycle, single-thread by construction — CLAV runs one APScheduler job and
processes watchlist symbols sequentially (docs/03), so "last call" is always the
call the gateway is about to persist. ``take()`` clears the buffer so a symbol
that produced no Gemini response (client error/timeout/budget-open, where the
sink never fires) can't accidentally inherit a previous symbol's record.
"""

from __future__ import annotations

from dataclasses import dataclass

from clav.integrations.llm.client import LLMResult
from clav.interfaces.analyst import AnalystSignal


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
