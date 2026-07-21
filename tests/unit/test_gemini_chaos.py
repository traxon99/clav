"""Story 3.11 — additional GeminiAnalyst chaos coverage: a JSON payload that
needs the regex-fallback extraction, a non-object JSON response, and a
provenance sink that itself raises. All must still degrade to a signal, never
propagate an exception out of ``analyze()``."""

from __future__ import annotations

from clav.integrations.llm import GeminiAnalyst, LLMResult


class FakeClient:
    def __init__(self, text: str) -> None:
        self._text = text

    def generate(self, prompt: str) -> LLMResult:
        return LLMResult(text=self._text, model="fake")


def test_json_object_embedded_in_prose_is_extracted_via_regex_fallback() -> None:
    text = (
        "Sure, here is my analysis: "
        '{"sentiment": 0.4, "conviction": 0.3, "rationale": "ok"} '
        "Let me know if you need anything else."
    )
    analyst = GeminiAnalyst(FakeClient(text))
    signal = analyst.analyze("AAPL", [], None, {})
    assert signal.is_fallback is False
    assert signal.sentiment == 0.4


def test_json_array_response_falls_back_to_neutral() -> None:
    analyst = GeminiAnalyst(FakeClient("[1, 2, 3]"))
    signal = analyst.analyze("AAPL", [], None, {})
    assert signal.is_fallback is True
    assert signal.llm_signal == 0.0


def test_provenance_sink_exception_does_not_break_analysis() -> None:
    def bad_sink(symbol, prompt, response_text, signal, usage) -> None:
        raise RuntimeError("sink db is down")

    client = FakeClient('{"sentiment": 0.2, "conviction": 0.2, "rationale": "x"}')
    analyst = GeminiAnalyst(client, provenance_sink=bad_sink)
    signal = analyst.analyze("AAPL", [], None, {})  # must not raise
    assert signal.is_fallback is False
    assert signal.sentiment == 0.2
