"""GeminiRestClient: request shape (model in the URL, thinkingConfig in the
body), the thoughtsTokenCount accounting fix (Google bills hidden reasoning
tokens as output too, but they don't land in candidatesTokenCount -- see
client.py's _parse_gemini_response), and the live reconfigure() used by the
analysis-effort preset toggle (services/scan_cycle.py)."""

from __future__ import annotations

import json

import httpx

from clav.integrations.llm.client import GeminiRestClient


def _client(handler, **kwargs) -> GeminiRestClient:
    transport = httpx.MockTransport(handler)
    return GeminiRestClient(api_key="k", client=httpx.Client(transport=transport), **kwargs)


def _response(text: str, *, thoughts_tokens: int = 0) -> httpx.Response:
    usage = {"promptTokenCount": 10, "candidatesTokenCount": 5}
    if thoughts_tokens:
        usage["thoughtsTokenCount"] = thoughts_tokens
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": usage,
        },
    )


def test_request_targets_configured_model_and_thinking_budget() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _response('{"a": 1}')

    # Arbitrary placeholder, not an endorsed model choice -- see LLMConfig.model
    # for why gemini-3.5-flash specifically isn't used anywhere in this app.
    client = _client(handler, model="some-model", thinking_budget=256)
    client.generate("prompt")

    assert len(captured) == 1
    assert captured[0].url.path.endswith("some-model:generateContent")
    body = json.loads(captured[0].content)
    assert body["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 256


def test_thoughts_tokens_fold_into_completion_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response('{"a": 1}', thoughts_tokens=134)

    client = _client(handler)
    result = client.generate("prompt")

    # 5 visible (candidatesTokenCount) + 134 hidden (thoughtsTokenCount) --
    # both billed as output tokens by Google, so both must count toward the
    # app's own daily_token_budget accounting.
    assert result.completion_tokens == 139
    assert result.prompt_tokens == 10
    assert result.total_tokens == 149


def test_reconfigure_changes_the_next_request() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _response('{"a": 1}')

    client = _client(handler, model="model-a", thinking_budget=512)
    client.generate("first")
    assert client.model == "model-a"
    assert client.thinking_budget == 512

    client.reconfigure(model="model-b", thinking_budget=0)
    client.generate("second")

    assert client.model == "model-b"
    assert client.thinking_budget == 0
    assert captured[0].url.path.endswith("model-a:generateContent")
    assert captured[1].url.path.endswith("model-b:generateContent")
    second_body = json.loads(captured[1].content)
    assert second_body["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 0
