"""ApeWisdomSource — keyless Reddit mention counts for the discovery funnel.

Reddit's own endpoints are 403 from this deployment (see the README's
social-source reality check), and OAuth is a multi-week approval. ApeWisdom
already does the scraping and exposes the aggregate for free, no key, no
account: ``api/v1.0/filter/{filter}/page/{n}`` over r/wallstreetbets, r/stocks,
r/options and friends.

What it gives us is ``mentions`` and ``mentions_24h_ago`` -- counts, **not post
text**. So this is strictly a ``DiscoverySource`` (which names are worth looking
at) and can never be a ``SocialSource``: there is nothing here for the sentiment
scorer to read, and no post to cite in a decision journal.

The 24h-ago figure is the point. ``StockTwitsTrendingSource`` ranks on absolute
popularity, which surfaces the same mega-caps every cycle; a name going from 8
mentions to 200 is the actual signal, and that delta is what this scores on.

Best-effort and fail-open like every other source.
"""

from __future__ import annotations

import json

from clav.common.logging import get_logger
from clav.domain.models import DiscoveryCandidate
from clav.integrations.news.http import HttpTextFetcher, TextFetcher
from clav.interfaces.discovery import DiscoverySource

_logger = get_logger(__name__)

_SOURCE = "apewisdom"
_URL = "https://apewisdom.io/api/v1.0/filter/{filter}/page/1"

# Growth multiple at/above which a name is flagged as a chatter spike. Mirrors
# SocialFilterParams.anomaly_volume_multiplier -- the analyst prompt already
# treats an anomaly as manipulation risk rather than a buy signal.
_SPIKE_MULTIPLE = 3.0
# Below this, "2 mentions -> 8 mentions" is noise, not a 4x breakout.
_MIN_MENTIONS_FOR_SPIKE = 10


def _as_int(value: object) -> int:
    """The API returns counts as JSON strings ("2", not 2)."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


class ApeWisdomSource(DiscoverySource):
    def __init__(
        self,
        *,
        subreddit_filter: str = "all-stocks",
        fetcher: TextFetcher | None = None,
    ) -> None:
        self._filter = subreddit_filter
        self._fetcher = fetcher or HttpTextFetcher()

    def fetch(self) -> list[DiscoveryCandidate]:
        try:
            body = self._fetcher.get(_URL.format(filter=self._filter))
            results = json.loads(body)["results"]
        except Exception as exc:  # fail-open: best-effort source
            _logger.warning("apewisdom_fetch_failed", filter=self._filter, error=str(exc))
            return []

        rows: list[tuple[str, int, int]] = []
        for entry in results:
            symbol = (entry.get("ticker") or "").strip().upper()
            if not symbol:
                continue
            rows.append(
                (
                    symbol,
                    _as_int(entry.get("mentions")),
                    _as_int(entry.get("mentions_24h_ago")),
                )
            )

        if not rows:
            return []

        peak_mentions = max(m for _, m, _ in rows) or 1
        peak_delta = max((max(0, m - prev) for _, m, prev in rows), default=0) or 1

        candidates: list[DiscoveryCandidate] = []
        for symbol, mentions, prev in rows:
            delta = max(0, mentions - prev)
            # Half presence, half acceleration: a name nobody mentions isn't
            # interesting however fast it grew, and a permanently-loud mega-cap
            # isn't interesting however loud it is.
            score = 0.5 * (mentions / peak_mentions) + 0.5 * (delta / peak_delta)
            spiking = (
                mentions >= _MIN_MENTIONS_FOR_SPIKE
                and prev > 0
                and mentions / prev >= _SPIKE_MULTIPLE
            )
            candidates.append(
                DiscoveryCandidate(
                    symbol=symbol,
                    score=round(min(1.0, score), 4),
                    mention_volume=mentions,
                    anomaly_flag=spiking,
                    source=_SOURCE,
                )
            )
        return candidates
