"""TradeReviewService — assembles the full provenance chain for one closed
trade into a ``ReviewContext`` (Story 5.3, docs/07-trade-review.md §2) and
runs the scheduled review pass that turns pending trades into persisted
``trade_review`` rows (Story 5.4).

``build_context()`` is read-only and bounded: it never writes anything and
never calls the ``Analyst``. The entry's sentiment/conviction/rationale/
model/``is_fallback`` are already embedded in ``decision.reasoning["llm"]`` by
Story 3.6 (``ScanCycleService``), so this reads from there directly rather
than a second round-trip to ``analysis_result`` -- the raw Gemini request/
response text stays out of the review context entirely, to avoid re-injecting
a large, adversarial-influenced blob and doubling the token cost of every
review.

``run_pass()`` is the scheduled entry point (its own ``APScheduler`` job,
separate from ``scan_cycle``/``daily_reset`` -- Story 5.4): it opens its own
session, walks every trade due for review, and for each one either persists a
``trade_review`` row, defers (budget/breaker), or fails the attempt with
exponential backoff. It never blocks, delays, or has any path to affect
``ScanCycleService``/``ExecutionEngine`` -- there is no order-submission path
anywhere in this file.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from clav.clock import Clock
from clav.common.logging import get_logger
from clav.data import tables
from clav.data.db import session_scope
from clav.data.repositories import Repositories
from clav.domain.models import SocialDigest
from clav.integrations.llm.budget import LLMBreakerOpen, LLMBudgetExceeded
from clav.integrations.llm.provenance import ReviewCapture
from clav.interfaces.analyst import Analyst, ReviewContext, ReviewedTrade, ReviewError

_logger = get_logger(__name__)

# Pi RAM discipline (matches web/calibration.py's MAX_TRADES / AnalystGateway's
# max_items_per_symbol): a review never loads a symbol's full news/candle
# history, only a bounded window around the trade itself.
DEFAULT_MAX_NEWS_ITEMS = 15
DEFAULT_MAX_CANDLES = 60

# Story 5.4 defaults (epic-05 decision #5); Story 5.7 will expose these as a
# `review:` config block. Off-peak-friendly: a failed attempt waits at least
# 5 minutes, backing off to at most 6 hours, and gives up after 5 tries.
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_BASE_SECONDS = 300.0
DEFAULT_BACKOFF_MAX_SECONDS = 21_600.0
DEFAULT_PASS_LIMIT = 50


def _summarize_social(digest: SocialDigest | None) -> str:
    if digest is None or digest.is_empty:
        return ""
    return (
        f"bull={digest.bull_count}, bear={digest.bear_count}, "
        f"bull_bear_ratio={digest.bull_bear_ratio:.2f}, "
        f"volume_ratio={digest.volume_ratio:.2f}, anomaly={digest.anomaly_flag}"
    )


class TradeReviewService:
    def __init__(
        self,
        *,
        analyst: Analyst,
        session_factory: sessionmaker[Session],
        clock: Clock,
        review_capture: ReviewCapture | None = None,
        max_news_items: int = DEFAULT_MAX_NEWS_ITEMS,
        max_candles: int = DEFAULT_MAX_CANDLES,
        candle_timeframe: str = "1Day",
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
        pass_limit: int = DEFAULT_PASS_LIMIT,
    ) -> None:
        # ``analyst`` is expected to be the SAME GeminiAnalyst instance (and
        # therefore the same GeminiBudget/breaker) that the entry-analysis
        # AnalystGateway uses (epic-05 decision #3) -- the composition root
        # is what makes that sharing real, not anything enforced here.
        self._analyst = analyst
        self._session_factory = session_factory
        self._clock = clock
        self._review_capture = review_capture
        self._max_news_items = max_news_items
        self._max_candles = max_candles
        self._candle_timeframe = candle_timeframe
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._pass_limit = pass_limit

    def run_pass(self) -> None:
        with session_scope(self._session_factory) as session:
            repos = Repositories(session)
            pending = repos.trades.list_pending_reviews(
                now=self._clock.now(), limit=self._pass_limit
            )
            for trade in pending:
                self._review_one(trade, repos)

    def _review_one(self, trade: tables.Trade, repos: Repositories) -> None:
        try:
            reviewed_trade = self._to_reviewed_trade(trade, repos)
            context = self.build_context(trade, repos)
        except Exception as exc:
            _logger.warning("trade_review_context_build_failed", trade_id=trade.id, error=str(exc))
            self._record_failure(trade, repos, exc)
            return

        try:
            review = self._analyst.review(reviewed_trade, context)
        except (LLMBudgetExceeded, LLMBreakerOpen) as exc:
            # Deferred, not failed (epic-05 decision #3): review_status and
            # review_attempts are both left untouched, so this trade is
            # simply retried -- with no backoff -- on the next pass.
            _logger.info("trade_review_deferred", trade_id=trade.id, reason=str(exc))
            return
        except ReviewError as exc:
            _logger.warning("trade_review_attempt_failed", trade_id=trade.id, error=str(exc))
            self._record_failure(trade, repos, exc)
            return
        except Exception as exc:  # defensive: Analyst.review()'s contract promises only ReviewError
            _logger.warning("trade_review_unexpected_error", trade_id=trade.id, error=str(exc))
            self._record_failure(trade, repos, exc)
            return

        raw_response: dict[str, str] = {}
        if self._review_capture is not None:
            record = self._review_capture.take()
            if record is not None:
                raw_response = {"request": record.request, "response": record.response}

        repos.trade_reviews.insert(
            trade.id,
            created_at=self._clock.now(),
            model=review.model,
            why_entered=review.why_entered,
            supporting_info=review.supporting_info,
            risks_at_entry=review.risks_at_entry,
            reasoning_correct=review.reasoning_correct,
            what_worked=review.what_worked,
            misleading_signals=review.misleading_signals,
            hindsight_view=review.hindsight_view,
            improvements=review.improvements,
            confidence_calibration=review.confidence_calibration,
            tags=review.tags,
            raw_response=raw_response,
        )
        repos.trades.mark_reviewed(trade.id)
        _logger.info("trade_review_recorded", trade_id=trade.id)

    def _record_failure(self, trade: tables.Trade, repos: Repositories, exc: Exception) -> None:
        attempts = trade.review_attempts + 1
        if attempts >= self._max_attempts:
            repos.trades.mark_review_failed(trade.id, attempts=attempts)
            _logger.warning(
                "trade_review_failed_terminally",
                trade_id=trade.id,
                attempts=attempts,
                error=str(exc),
            )
            return
        backoff_seconds = min(
            self._backoff_base_seconds * (2 ** (attempts - 1)), self._backoff_max_seconds
        )
        next_attempt_at = self._clock.now() + timedelta(seconds=backoff_seconds)
        repos.trades.mark_review_attempt_failed(
            trade.id, attempts=attempts, next_attempt_at=next_attempt_at
        )

    def _to_reviewed_trade(self, trade: tables.Trade, repos: Repositories) -> ReviewedTrade:
        instrument = repos.instruments.get_by_id(trade.instrument_id)
        return ReviewedTrade(
            id=trade.id,
            symbol=instrument.symbol if instrument is not None else "",
            qty=trade.qty,
            entry_price=trade.entry_price,
            exit_price=trade.exit_price or 0.0,
            opened_at=trade.opened_at,
            closed_at=trade.closed_at or trade.opened_at,
            realized_pl=trade.realized_pl or 0.0,
            return_pct=trade.return_pct or 0.0,
        )

    def build_context(self, trade: tables.Trade, repos: Repositories) -> ReviewContext:
        decision = (
            repos.decisions.get(trade.entry_decision_id)
            if trade.entry_decision_id is not None
            else None
        )
        risk_eval = (
            repos.risk_evaluations.get_by_decision_id(trade.entry_decision_id)
            if trade.entry_decision_id is not None
            else None
        )
        # No "llm" key at all (analyst never wired) is functionally the same
        # as an explicit is_fallback=True: either way the entry had no real
        # LLM analysis behind it.
        llm_provenance: dict[str, Any] = (
            decision.reasoning.get("llm", {}) if decision is not None else {}
        )

        return ReviewContext(
            entry_action=decision.action if decision is not None else "BUY",
            raw_score=decision.raw_score if decision is not None else 0.0,
            technical_score=decision.technical_score if decision is not None else 0.0,
            llm_signal=decision.llm_signal if decision is not None else 0.0,
            is_fallback=bool(llm_provenance.get("is_fallback", True)),
            entry_rationale=decision.reasoning if decision is not None else {},
            risk_notes=risk_eval.notes if risk_eval is not None else {},
            news_headlines=self._news_headlines(repos, llm_provenance.get("news_item_ids", [])),
            social_summary=self._social_summary(repos, llm_provenance.get("social_digest_id")),
            price_path=self._price_path(repos, trade),
            exit_reason=self._derive_exit_reason(repos, trade),
        )

    def _news_headlines(self, repos: Repositories, news_item_ids: list[int]) -> list[str]:
        headlines: list[str] = []
        for item_id in news_item_ids[: self._max_news_items]:
            item = repos.news_items.get_by_id(item_id)
            if item is not None:
                headlines.append(item.headline)
        return headlines

    def _social_summary(self, repos: Repositories, digest_id: int | None) -> str:
        if digest_id is None:
            return ""
        return _summarize_social(repos.social_digests.get_by_id(digest_id))

    def _price_path(self, repos: Repositories, trade: tables.Trade) -> list[float]:
        if trade.closed_at is None:
            return []
        candles = repos.candles.get_range(
            trade.instrument_id,
            self._candle_timeframe,
            start=trade.opened_at,
            end=trade.closed_at,
            limit=self._max_candles,
        )
        return [c.close for c in candles]

    def _derive_exit_reason(self, repos: Repositories, trade: tables.Trade) -> str:
        """epic-05 decision #4: derived from the existing order/decision/
        risk_evaluation joins, no ``exit_reason`` column. Only "signal" and
        "stop_monitor" are reachable today -- "risk_forced" (flatten-on-
        estop) isn't wired yet (README's runbook notes this has no effect
        yet), and an approval-mode rejection/expiry applies only to an entry
        that never opens a ``Trade`` row in the first place, so neither can
        actually describe an exit."""
        if trade.exit_order_id is None:
            return "unknown"
        exit_order = repos.orders.get(trade.exit_order_id)
        if exit_order is None or exit_order.decision_id is None:
            return "unknown"
        exit_decision = repos.decisions.get(exit_order.decision_id)
        if exit_decision is None:
            return "unknown"
        risk_eval = repos.risk_evaluations.get_by_decision_id(exit_decision.id)
        if risk_eval is not None and risk_eval.notes.get("source") == "stop_monitor":
            return "stop_monitor"
        if exit_decision.action == "SELL":
            return "signal"
        return "unknown"
