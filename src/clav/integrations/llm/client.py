"""LLM client protocol + a keyless-until-configured Gemini REST client (Story 3.4).

The analyst depends only on the tiny ``LLMClient`` protocol, so tests inject a
fake and no network is touched in CI. ``GeminiRestClient`` speaks the Generative
Language REST API over ``httpx`` (no Google SDK dependency) and reports token
usage for the Story-3.5 budget accountant. The API key is passed as a header and
is never logged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from clav.common.retry import retry_transient

DEFAULT_MODEL = "gemini-1.5-flash"
_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


@dataclass(frozen=True)
class LLMResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMBlockedError(Exception):
    """Raised when the provider blocks a prompt/response (safety filter, empty
    candidates). The analyst turns this into a neutral signal."""


class LLMClient(Protocol):
    def generate(self, prompt: str) -> LLMResult:  # pragma: no cover - protocol
        ...


class GeminiRestClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout: float = 20.0,
        max_output_tokens: int = 1024,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_output_tokens = max_output_tokens
        self._client = client or httpx.Client(timeout=timeout)

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    @retry_transient()
    def generate(self, prompt: str) -> LLMResult:
        if not self._api_key:
            raise LLMBlockedError("gemini api key not configured")
        url = _ENDPOINT.format(model=self._model)
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": self._max_output_tokens,
                "responseMimeType": "application/json",
            },
        }
        resp = self._client.post(
            url,
            headers={"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
            content=json.dumps(body),
        )
        resp.raise_for_status()
        payload = resp.json()
        return _parse_gemini_response(payload, self._model)


def _parse_gemini_response(payload: dict[str, Any], model: str) -> LLMResult:
    candidates = payload.get("candidates") or []
    if not candidates:
        reason = (payload.get("promptFeedback") or {}).get("blockReason", "no candidates")
        raise LLMBlockedError(f"gemini returned no candidates: {reason}")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise LLMBlockedError("gemini returned an empty candidate")
    usage = payload.get("usageMetadata") or {}
    return LLMResult(
        text=text,
        prompt_tokens=int(usage.get("promptTokenCount", 0) or 0),
        completion_tokens=int(usage.get("candidatesTokenCount", 0) or 0),
        model=model,
    )
