"""TradeReviewService — assembles the full provenance chain for one closed
trade into a ``ReviewContext`` (Story 5.3, docs/07-trade-review.md §2).

Read-only and bounded: this never writes anything and never calls the
``Analyst`` -- it is purely the "gather everything a human analyst would
want" step. Story 5.4 adds the scheduled pass that calls ``Analyst.review()``
with the ``ReviewedTrade``/``ReviewContext`` this builds, under the shared
``GeminiBudget``.

The entry's sentiment/conviction/rationale/model/``is_fallback`` are already
embedded in ``decision.reasoning["llm"]`` by Story 3.6 (``ScanCycleService``),
so this reads from there directly rather than a second round-trip to
``analysis_result`` -- the raw Gemini request/response text stays out of the
review context entirely, to avoid re-injecting a large, adversarial-
influenced blob and doubling the token cost of every review.
"""

from __future__ import annotations

from typing import Any

from clav.data import tables
from clav.data.repositories import Repositories
from clav.domain.models import SocialDigest
from clav.interfaces.analyst import ReviewContext

# Pi RAM discipline (matches web/calibration.py's MAX_TRADES / AnalystGateway's
# max_items_per_symbol): a review never loads a symbol's full news/candle
# history, only a bounded window around the trade itself.
DEFAULT_MAX_NEWS_ITEMS = 15
DEFAULT_MAX_CANDLES = 60


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
        max_news_items: int = DEFAULT_MAX_NEWS_ITEMS,
        max_candles: int = DEFAULT_MAX_CANDLES,
        candle_timeframe: str = "1Day",
    ) -> None:
        self._max_news_items = max_news_items
        self._max_candles = max_candles
        self._candle_timeframe = candle_timeframe

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
