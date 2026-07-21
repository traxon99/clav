"""Gemini token/cost budget + circuit breaker (Story 3.5).

Two independent guards keep the LLM from blowing the (free-tier) budget or hanging
the loop, both tripping to **technical-only**:

- **Budget accountant** — a rolling-daily token + cost tally. Exceeding
  ``daily_token_budget`` or ``daily_cost_cap_usd`` disables Gemini for the rest of
  the day (until the ``daily_reset`` job zeroes the counters).
- **Circuit breaker** — opens after ``failure_threshold`` consecutive
  failures/timeouts and auto half-opens after ``cooldown_seconds`` (one trial
  call); a success closes it, a failure re-opens it.

``GuardedLLMClient`` wraps any ``LLMClient`` with these guards: when a guard is
tripped it raises ``LLMBudgetExceeded`` / ``LLMBreakerOpen`` — which
``GeminiAnalyst`` already turns into a neutral signal. Time comes from an injected
``Clock`` so everything is deterministic under ``FakeClock``. Conventions:
``daily_token_budget == 0`` disables Gemini entirely; ``daily_cost_cap_usd <= 0``
means *no monetary cap* (the free-tier default, where per-token cost is 0).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from clav.clock import Clock
from clav.common.logging import get_logger
from clav.integrations.llm.client import LLMClient, LLMResult

_logger = get_logger(__name__)


class LLMBudgetExceeded(Exception):
    """Raised by GuardedLLMClient when the daily token/cost budget is spent."""


class LLMBreakerOpen(Exception):
    """Raised by GuardedLLMClient when the circuit breaker is open (cooling down)."""


class GeminiBudget:
    def __init__(
        self,
        *,
        clock: Clock,
        daily_token_budget: int,
        daily_cost_cap_usd: float,
        failure_threshold: int,
        cooldown_seconds: int,
        cost_per_1k_prompt_tokens_usd: float = 0.0,
        cost_per_1k_completion_tokens_usd: float = 0.0,
    ) -> None:
        self._clock = clock
        self._daily_token_budget = daily_token_budget
        self._daily_cost_cap_usd = daily_cost_cap_usd
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._cost_p = cost_per_1k_prompt_tokens_usd
        self._cost_c = cost_per_1k_completion_tokens_usd

        self._daily_tokens = 0
        self._daily_cost = 0.0
        self._calls_today = 0
        self._consecutive_failures = 0
        self._breaker_open = False
        self._breaker_opened_at: datetime | None = None

    # --- gates -----------------------------------------------------------------
    def budget_exhausted(self) -> bool:
        if self._daily_token_budget == 0:
            return True  # 0 budget disables Gemini entirely
        if self._daily_tokens >= self._daily_token_budget:
            return True
        return self._daily_cost_cap_usd > 0 and self._daily_cost >= self._daily_cost_cap_usd

    def breaker_blocking(self) -> bool:
        """Open breaker blocks unless the cooldown has elapsed (then half-open:
        one trial call is allowed through)."""
        if not self._breaker_open or self._breaker_opened_at is None:
            return False
        elapsed = (self._clock.now() - self._breaker_opened_at).total_seconds()
        return elapsed < self._cooldown_seconds

    def can_call(self) -> bool:
        return not self.budget_exhausted() and not self.breaker_blocking()

    # --- accounting ------------------------------------------------------------
    def record_success(self, result: LLMResult) -> None:
        self._daily_tokens += result.total_tokens
        self._daily_cost += (
            result.prompt_tokens / 1000.0 * self._cost_p
            + result.completion_tokens / 1000.0 * self._cost_c
        )
        self._calls_today += 1
        if self._breaker_open:
            _logger.info("gemini_breaker_closed")
        self._breaker_open = False
        self._breaker_opened_at = None
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        self._calls_today += 1
        if (
            not self._breaker_open
            and self._consecutive_failures >= self._failure_threshold
        ):
            self._breaker_open = True
            self._breaker_opened_at = self._clock.now()
            _logger.warning(
                "gemini_breaker_opened", consecutive_failures=self._consecutive_failures
            )
        elif self._breaker_open:
            # Failed trial call in half-open state: re-arm the cooldown.
            self._breaker_opened_at = self._clock.now()

    def reset_daily(self) -> None:
        """Zero the rolling-daily counters (called by the daily_reset job). The
        breaker state is intentionally left alone — it self-heals on its own
        cooldown, not on the calendar."""
        self._daily_tokens = 0
        self._daily_cost = 0.0
        self._calls_today = 0

    def snapshot(self) -> dict[str, Any]:
        """State for the control API / health endpoint (Story 3.8)."""
        return {
            "daily_tokens": self._daily_tokens,
            "daily_token_budget": self._daily_token_budget,
            "daily_cost_usd": round(self._daily_cost, 6),
            "daily_cost_cap_usd": self._daily_cost_cap_usd,
            "calls_today": self._calls_today,
            "budget_exhausted": self.budget_exhausted(),
            "breaker_open": self._breaker_open,
            "consecutive_failures": self._consecutive_failures,
            "llm_enabled": self.can_call(),
        }


class GuardedLLMClient(LLMClient):
    """Wraps an ``LLMClient`` with the budget accountant + circuit breaker."""

    def __init__(self, inner: LLMClient, budget: GeminiBudget) -> None:
        self._inner = inner
        self._budget = budget

    def generate(self, prompt: str) -> LLMResult:
        if self._budget.budget_exhausted():
            raise LLMBudgetExceeded("gemini daily token/cost budget exhausted")
        if self._budget.breaker_blocking():
            raise LLMBreakerOpen("gemini circuit breaker open (cooling down)")
        try:
            result = self._inner.generate(prompt)
        except Exception:
            self._budget.record_failure()
            raise
        self._budget.record_success(result)
        return result
