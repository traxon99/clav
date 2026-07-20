"""Story 3.5 — token budget, cost cap & circuit breaker (FakeClock)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from clav.clock import FakeClock
from clav.integrations.llm import (
    GeminiAnalyst,
    GeminiBudget,
    GuardedLLMClient,
    LLMBreakerOpen,
    LLMBudgetExceeded,
    LLMResult,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _budget(clock, **kw) -> GeminiBudget:
    defaults = dict(
        daily_token_budget=1000,
        daily_cost_cap_usd=0.0,
        failure_threshold=3,
        cooldown_seconds=900,
    )
    defaults.update(kw)
    return GeminiBudget(clock=clock, **defaults)


class OkClient:
    def __init__(self, tokens: int = 100) -> None:
        self.calls = 0
        self._tokens = tokens

    def generate(self, prompt: str) -> LLMResult:
        self.calls += 1
        return LLMResult(text="{}", prompt_tokens=self._tokens, completion_tokens=0, model="m")


class FailClient:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str) -> LLMResult:
        self.calls += 1
        raise TimeoutError("boom")


def test_token_budget_exhaustion_disables_calls() -> None:
    clock = FakeClock(NOW)
    budget = _budget(clock, daily_token_budget=250)
    inner = OkClient(tokens=100)
    client = GuardedLLMClient(inner, budget)

    client.generate("p")  # 100
    client.generate("p")  # 200
    assert budget.budget_exhausted() is False
    client.generate("p")  # 300 -> now over 250
    assert budget.budget_exhausted() is True
    with pytest.raises(LLMBudgetExceeded):
        client.generate("p")
    assert inner.calls == 3  # the 4th never reached the inner client


def test_zero_budget_disables_entirely() -> None:
    budget = _budget(FakeClock(NOW), daily_token_budget=0)
    client = GuardedLLMClient(OkClient(), budget)
    assert budget.can_call() is False
    with pytest.raises(LLMBudgetExceeded):
        client.generate("p")


def test_cost_cap_disables_calls() -> None:
    clock = FakeClock(NOW)
    budget = _budget(
        clock,
        daily_token_budget=10_000_000,
        daily_cost_cap_usd=0.01,
        cost_per_1k_prompt_tokens_usd=1.0,  # 1000 prompt tokens = $1
    )
    client = GuardedLLMClient(OkClient(tokens=100), budget)
    client.generate("p")  # 100 prompt tokens = $0.10 > $0.01 cap
    assert budget.budget_exhausted() is True
    with pytest.raises(LLMBudgetExceeded):
        client.generate("p")


def test_free_tier_cost_cap_zero_means_no_cap() -> None:
    budget = _budget(FakeClock(NOW), daily_token_budget=10_000_000, daily_cost_cap_usd=0.0)
    client = GuardedLLMClient(OkClient(tokens=100), budget)
    for _ in range(5):
        client.generate("p")
    assert budget.budget_exhausted() is False


def test_breaker_opens_after_threshold_failures() -> None:
    clock = FakeClock(NOW)
    budget = _budget(clock, failure_threshold=3)
    inner = FailClient()
    client = GuardedLLMClient(inner, budget)

    for _ in range(3):
        with pytest.raises(TimeoutError):
            client.generate("p")
    assert budget.breaker_blocking() is True
    # Now the breaker short-circuits before reaching the inner client.
    with pytest.raises(LLMBreakerOpen):
        client.generate("p")
    assert inner.calls == 3


def test_breaker_half_opens_after_cooldown_and_closes_on_success() -> None:
    clock = FakeClock(NOW)
    budget = _budget(clock, failure_threshold=2, cooldown_seconds=900)
    fail = FailClient()
    guarded_fail = GuardedLLMClient(fail, budget)
    for _ in range(2):
        with pytest.raises(TimeoutError):
            guarded_fail.generate("p")
    assert budget.breaker_blocking() is True

    # Before cooldown: still blocked.
    clock.set(NOW + timedelta(seconds=800))
    assert budget.breaker_blocking() is True

    # After cooldown: half-open, a trial call is allowed; success closes it.
    clock.set(NOW + timedelta(seconds=901))
    assert budget.can_call() is True
    ok = OkClient()
    GuardedLLMClient(ok, budget).generate("p")
    assert budget.breaker_blocking() is False
    assert ok.calls == 1


def test_daily_reset_re_enables_after_budget_exhaustion() -> None:
    clock = FakeClock(NOW)
    budget = _budget(clock, daily_token_budget=150)
    client = GuardedLLMClient(OkClient(tokens=100), budget)
    client.generate("p")
    client.generate("p")
    assert budget.budget_exhausted() is True

    budget.reset_daily()
    assert budget.budget_exhausted() is False
    snap = budget.snapshot()
    assert snap["daily_tokens"] == 0
    assert snap["llm_enabled"] is True


def test_guarded_analyst_degrades_to_neutral_when_disabled() -> None:
    budget = _budget(FakeClock(NOW), daily_token_budget=0)
    guarded = GuardedLLMClient(OkClient(), budget)
    analyst = GeminiAnalyst(guarded)
    signal = analyst.analyze("AAPL", [], None, {})
    assert signal.is_fallback is True
    assert signal.llm_signal == 0.0
