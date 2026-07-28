"""StockTwitsTrendingSource — public, unauthenticated "trending symbols" feed
(autonomous-discovery epic).

Reads ``trending/symbols.json`` (keyless): one call returns the ~30 most-active
cashtags right now, each with a ``watchlist_count`` we normalize into a [0, 1]
buzz score. This is the cheap market-wide pre-filter that seeds the discovery
funnel — no per-symbol fan-out. Same caveats as ``StockTwitsSource``: new API
registrations are paused as of 2026-07 and the public endpoint is unstable, so
this is strictly best-effort and fail-open.
"""

from __future__ import annotations

import json

from clav.common.logging import get_logger
from clav.domain.models import DiscoveryCandidate
from clav.integrations.news.http import HttpTextFetcher, TextFetcher
from clav.integrations.source_health import FailOpenSource
from clav.interfaces.discovery import DiscoverySource

_logger = get_logger(__name__)

_SOURCE = "stocktwits_trending"


class StockTwitsTrendingSource(FailOpenSource, DiscoverySource):
    source_name = "stocktwits_trending"

    def __init__(
        self,
        *,
        url: str = "https://api.stocktwits.com/api/2/trending/symbols.json",
        fetcher: TextFetcher | None = None,
    ) -> None:
        self._url = url
        self._fetcher = fetcher or HttpTextFetcher()

    def fetch(self) -> list[DiscoveryCandidate]:
        try:
            body = self._fetcher.get(self._url)
            payload = json.loads(body)
            symbols = payload["symbols"]
        except Exception as exc:  # fail-open: best-effort source
            self._record_failure(exc)
            _logger.warning("stocktwits_trending_fetch_failed", error=str(exc))
            return []

        raw: list[tuple[str, int]] = []
        for entry in symbols:
            symbol = (entry.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            volume = int(entry.get("watchlist_count", 0) or 0)
            raw.append((symbol, volume))

        if not raw:
            self._record_ok(0)
            return []

        peak = max(volume for _, volume in raw) or 1
        self._record_ok(len(raw))
        return [
            DiscoveryCandidate(
                symbol=symbol,
                score=round(volume / peak, 4),
                mention_volume=volume,
                anomaly_flag=False,
                source=_SOURCE,
            )
            for symbol, volume in raw
        ]
