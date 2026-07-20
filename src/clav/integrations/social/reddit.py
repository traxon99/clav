"""RedditSource — public, unauthenticated Reddit search (free, keyless, Story 3.2).

Uses Reddit's public ``search.json`` endpoint (the un-monetized read path the
website itself uses) with a descriptive User-Agent. OAuth (100 QPM) is an optional
future upgrade if/when ticket-and-wait pre-approval is granted — never required.

Reputation note: the public search listing does **not** expose per-author karma,
so ``author_reputation`` falls back to ``default_author_reputation`` unless the
payload carries an ``author_karma`` field. The reputation floor is therefore
best-effort on Reddit; engagement floor, promo filter, and near-dup collapse do
the heavy lifting here, and StockTwits supplies real follower-count reputation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from clav.clock import Clock
from clav.common.logging import get_logger
from clav.domain.models import Engagement, SocialItem
from clav.integrations.news.http import HttpTextFetcher, TextFetcher
from clav.interfaces.social import SocialSource

_logger = get_logger(__name__)

DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing")


class RedditSource(SocialSource):
    def __init__(
        self,
        *,
        clock: Clock,
        subreddits: tuple[str, ...] = DEFAULT_SUBREDDITS,
        default_author_reputation: float = 1000.0,
        fetcher: TextFetcher | None = None,
    ) -> None:
        self._clock = clock
        self._subreddits = subreddits
        self._default_reputation = default_author_reputation
        self._fetcher = fetcher or HttpTextFetcher()

    def _search_url(self, subreddit: str, symbol: str) -> str:
        return (
            f"https://www.reddit.com/r/{subreddit}/search.json"
            f"?q=%24{symbol}&restrict_sr=1&sort=new&limit=50"
        )

    def fetch(self, symbol: str, since: datetime) -> list[SocialItem]:
        symbol = symbol.upper()
        items: list[SocialItem] = []
        for subreddit in self._subreddits:
            items.extend(self._fetch_subreddit(subreddit, symbol, since))
        return items

    def _fetch_subreddit(self, subreddit: str, symbol: str, since: datetime) -> list[SocialItem]:
        try:
            body = self._fetcher.get(self._search_url(subreddit, symbol))
            payload = json.loads(body)
            children = payload["data"]["children"]
        except Exception as exc:  # fail-open: best-effort source
            _logger.warning(
                "reddit_fetch_failed", symbol=symbol, subreddit=subreddit, error=str(exc)
            )
            return []

        items: list[SocialItem] = []
        for child in children:
            data = child.get("data", {})
            title = (data.get("title") or "").strip()
            selftext = (data.get("selftext") or "").strip()
            text = f"{title}\n{selftext}".strip()
            if not text:
                continue
            posted_at = datetime.fromtimestamp(float(data.get("created_utc", 0)), tz=UTC)
            if posted_at < since:
                continue
            reputation = data.get("author_karma")
            items.append(
                SocialItem(
                    symbol=symbol,
                    text=text,
                    author=data.get("author") or "unknown",
                    author_reputation=(
                        float(reputation) if reputation is not None else self._default_reputation
                    ),
                    engagement=Engagement(
                        score=int(data.get("score", 0) or 0),
                        replies=int(data.get("num_comments", 0) or 0),
                    ),
                    posted_at=posted_at,
                    source=f"reddit:{subreddit}",
                    sentiment=None,
                )
            )
        return items
