"""GeminiAnalyst — strict-JSON signal with Stage-2 judgement + neutral fallback
(Story 3.4), and a strict-JSON trade-review post-mortem (Story 5.2).

The cardinal rule for ``analyze()``: **this never raises because of the model.**
Any client error, timeout, safety block, malformed JSON, schema violation, or
out-of-range value is caught and turned into ``AnalystSignal.neutral(...)`` so
the scan cycle degrades to technical-only. Every call is offered to an optional
provenance sink (redacted prompt/response), which Story 3.12 wires to
persistence.

``review()`` is the deliberate opposite: there is no safe neutral trade review
(epic-05 decision #3), so the same failure modes are re-raised as a typed
``ReviewError`` instead of being swallowed. Its own provenance sink only fires
on success -- a failed attempt is logged, never persisted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from clav.common.logging import get_logger
from clav.integrations.llm.client import LLMClient, LLMResult
from clav.integrations.llm.prompt import DEFAULT_PERSONA, build_prompt, build_review_prompt
from clav.interfaces.analyst import (
    Analyst,
    AnalystSignal,
    ReviewContext,
    ReviewedTrade,
    ReviewError,
    TradeReview,
)
from clav.interfaces.news import NewsItem
from clav.interfaces.social import SocialDigest

_logger = get_logger(__name__)

# (persona_content, prompt_version) — Story 3.10 supplies a versioned, hot-reloaded
# provider; the default is a static built-in persona so a fresh install works.
PersonaProvider = Callable[[], "tuple[str, str | None]"]

# Sink for provenance (Story 3.12): (symbol, prompt, raw_response, signal, usage).
ProvenanceSink = Callable[[str, str, str, AnalystSignal, LLMResult], None]

# Sink for review provenance (Story 5.2): (trade_id, prompt, raw_response, review,
# usage) -- fires on success only; a failed attempt raises ReviewError instead.
ReviewProvenanceSink = Callable[[int, str, str, TradeReview, LLMResult], None]

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _default_persona_provider() -> tuple[str, str | None]:
    return DEFAULT_PERSONA, None


def _extract_json(text: str) -> dict[str, Any]:
    """Tolerate a model that wraps its JSON in prose/code fences: pull the first
    balanced-looking object out before parsing."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(text)
        if match is None:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("model output was not a JSON object")
    return parsed


class GeminiAnalyst(Analyst):
    def __init__(
        self,
        client: LLMClient,
        *,
        persona_provider: PersonaProvider | None = None,
        provenance_sink: ProvenanceSink | None = None,
        review_provenance_sink: ReviewProvenanceSink | None = None,
    ) -> None:
        self._client = client
        self._persona_provider = persona_provider or _default_persona_provider
        self._provenance_sink = provenance_sink
        self._review_provenance_sink = review_provenance_sink

    def analyze(
        self,
        symbol: str,
        news: list[NewsItem],
        social_digest: SocialDigest | None,
        context: dict[str, Any],
    ) -> AnalystSignal:
        persona, prompt_version = self._persona_provider()
        prompt = build_prompt(
            persona=persona,
            symbol=symbol,
            news=news,
            social_digest=social_digest,
            context=context,
        )
        try:
            result = self._client.generate(prompt)
        except Exception as exc:  # client/network/timeout/safety-block
            _logger.warning("gemini_call_failed", symbol=symbol, error=str(exc))
            return AnalystSignal.neutral(str(exc), prompt_version=prompt_version)

        try:
            data = _extract_json(result.text)
            signal = AnalystSignal.model_validate(
                {
                    "sentiment": data.get("sentiment"),
                    "conviction": data.get("conviction"),
                    "catalysts": data.get("catalysts", []),
                    "rationale": data.get("rationale", ""),
                    "model": result.model,
                    "prompt_version": prompt_version,
                }
            )
        except Exception as exc:  # invalid JSON / schema / out-of-range
            _logger.warning("gemini_response_invalid", symbol=symbol, error=str(exc))
            signal = AnalystSignal.neutral(
                f"invalid response: {exc}", model=result.model, prompt_version=prompt_version
            )

        self._record(symbol, prompt, result, signal)
        return signal

    def _record(self, symbol: str, prompt: str, result: LLMResult, signal: AnalystSignal) -> None:
        if self._provenance_sink is None:
            return
        try:
            self._provenance_sink(symbol, prompt, result.text, signal, result)
        except Exception as exc:  # provenance must never break the cycle
            _logger.warning("gemini_provenance_sink_failed", symbol=symbol, error=str(exc))

    def review(self, trade: ReviewedTrade, context: ReviewContext) -> TradeReview:
        persona, _prompt_version = self._persona_provider()
        prompt = build_review_prompt(persona=persona, trade=trade, context=context)

        try:
            result = self._client.generate(prompt)
        except Exception as exc:  # client/network/timeout/safety-block
            _logger.warning("gemini_review_call_failed", trade_id=trade.id, error=str(exc))
            raise ReviewError(f"review call failed: {exc}") from exc

        try:
            data = _extract_json(result.text)
            review = TradeReview.model_validate({**data, "model": result.model})
        except Exception as exc:  # invalid JSON / schema / enum violation
            _logger.warning("gemini_review_invalid", trade_id=trade.id, error=str(exc))
            raise ReviewError(f"invalid review response: {exc}") from exc

        self._record_review(trade.id, prompt, result, review)
        return review

    def _record_review(
        self, trade_id: int, prompt: str, result: LLMResult, review: TradeReview
    ) -> None:
        if self._review_provenance_sink is None:
            return
        try:
            self._review_provenance_sink(trade_id, prompt, result.text, review, result)
        except Exception as exc:  # provenance must never break the caller
            _logger.warning(
                "gemini_review_provenance_sink_failed", trade_id=trade_id, error=str(exc)
            )
