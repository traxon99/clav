"""AnalystGateway — turns the raw news/social sources into a single, bounded
``AnalystSignal`` for one symbol, with dedup/cache/storage and provenance refs
(Stories 3.1-3.6).

This is the orchestration layer that sits between the fail-open adapters and the
decision engine:

1. Fetch news from every configured ``NewsSource`` (TTL-guarded), persist +
   dedup via ``NewsItemRepository``, then read back the fresh (non-stale) set.
2. Fetch social from every ``SocialSource``, run the deterministic Stage-1 filter
   + aggregation (``domain.social.build_digest``) against the rolling baseline,
   and persist the resulting ``SocialDigest``.
3. Ask the ``Analyst`` for a signal over that compact digest. Any failure inside a
   source is already swallowed by the adapter; the analyst itself never raises
   (neutral fallback). So the worst case here is a neutral signal — the cycle
   degrades to technical-only, never aborts.

The returned ``GatewayResult`` carries the signal plus the input row-ids so the
decision journal (Story 3.7) and provenance chain (Story 3.12) can reconstruct
*why* a trade happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from clav.clock import Clock
from clav.common.cache import TtlCache
from clav.common.logging import get_logger
from clav.data.repositories import Repositories
from clav.domain.models import SocialDigest, SocialItem
from clav.domain.social import SocialFilterParams, build_digest
from clav.integrations.llm.budget import GeminiBudget
from clav.interfaces.analyst import Analyst, AnalystSignal
from clav.interfaces.news import NewsSource
from clav.interfaces.social import SocialSource

_logger = get_logger(__name__)


@dataclass(frozen=True)
class GatewayResult:
    signal: AnalystSignal
    news_item_ids: list[int] = field(default_factory=list)
    social_digest_id: int | None = None


class AnalystGateway:
    def __init__(
        self,
        *,
        analyst: Analyst,
        news_sources: list[NewsSource],
        social_sources: list[SocialSource],
        filter_params: SocialFilterParams,
        clock: Clock,
        cache: TtlCache,
        max_age_hours: int,
        max_items_per_symbol: int,
        social_baseline_window: int,
        reset_daily_hook: Any = None,
        budget: GeminiBudget | None = None,
    ) -> None:
        self._analyst = analyst
        self._news_sources = news_sources
        self._social_sources = social_sources
        self._filter_params = filter_params
        self._clock = clock
        self._cache = cache
        self._max_age_hours = max_age_hours
        self._max_items_per_symbol = max_items_per_symbol
        self._social_baseline_window = social_baseline_window
        # Called by ScanCycleService.daily_reset (Story 3.5 counters reset).
        self._reset_daily_hook = reset_daily_hook
        # Optional reference purely for /health reporting (Story 3.8) --
        # ScanCycleService persists this snapshot to system_control each cycle
        # so the separate clav-web process can read it without touching
        # clav-core's in-memory state directly.
        self._budget = budget

    def reset_daily(self) -> None:
        if self._reset_daily_hook is not None:
            self._reset_daily_hook()

    def budget_snapshot(self) -> dict[str, Any] | None:
        return self._budget.snapshot() if self._budget is not None else None

    def signal_for(
        self,
        symbol: str,
        repos: Repositories,
        instrument_id: int,
        *,
        context: dict[str, Any] | None = None,
        is_low_liquidity: bool = False,
    ) -> GatewayResult:
        context = context or {}
        now = self._clock.now()
        since = now - timedelta(hours=self._max_age_hours)

        self._ingest_news(symbol, repos, instrument_id, since)
        news = repos.news_items.get_for_analysis(
            instrument_id,
            now=now,
            max_age_hours=self._max_age_hours,
            limit=self._max_items_per_symbol,
        )
        news_ids = repos.news_items.recent_ids_for_analysis(
            instrument_id,
            now=now,
            max_age_hours=self._max_age_hours,
            limit=self._max_items_per_symbol,
        )

        digest_id = None
        digest = None
        if self._social_sources:
            digest, digest_id = self._ingest_social(
                symbol, repos, instrument_id, since, is_low_liquidity
            )

        signal = self._analyst.analyze(symbol, news, digest, context)
        _logger.info(
            "analyst_signal",
            symbol=symbol,
            sentiment=signal.sentiment,
            conviction=signal.conviction,
            llm_signal=signal.llm_signal,
            is_fallback=signal.is_fallback,
            news_count=len(news),
            social_anomaly=(digest.anomaly_flag if digest is not None else None),
        )
        return GatewayResult(signal=signal, news_item_ids=news_ids, social_digest_id=digest_id)

    def _ingest_news(
        self, symbol: str, repos: Repositories, instrument_id: int, since: datetime
    ) -> None:
        for source in self._news_sources:
            key = f"news:{source.__class__.__name__}:{symbol}"
            if not self._cache.should_fetch(key):
                continue
            items = source.fetch(symbol, since)
            self._cache.mark(key)
            if items:
                repos.news_items.add_many(instrument_id, items)
        repos.news_items.prune(instrument_id, keep=self._max_items_per_symbol)

    def _ingest_social(
        self,
        symbol: str,
        repos: Repositories,
        instrument_id: int,
        since: datetime,
        is_low_liquidity: bool,
    ) -> tuple[SocialDigest, int]:
        items: list[SocialItem] = []
        for source in self._social_sources:
            key = f"social:{source.__class__.__name__}:{symbol}"
            if not self._cache.should_fetch(key):
                continue
            items.extend(source.fetch(symbol, since))
            self._cache.mark(key)

        baseline = repos.social_digests.rolling_baseline(
            instrument_id, window=self._social_baseline_window
        )
        digest = build_digest(
            symbol,
            items,
            baseline_volume=baseline,
            params=self._filter_params,
            now=self._clock.now(),
            is_low_liquidity=is_low_liquidity,
        )
        digest_id = repos.social_digests.add(instrument_id, digest)
        repos.social_digests.prune(instrument_id, keep=self._max_items_per_symbol)
        return digest, digest_id
