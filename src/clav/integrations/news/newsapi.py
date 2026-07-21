"""NewsApiSource — optional paid-tier news, **off by default** (Story 3.1, decision #5).

Only active when an API key is configured; absence of a key is **not** an error —
the adapter simply returns an empty list so a fresh clone with no paid keys runs
the full loop. The key is passed as a request header (never logged, never placed
in a URL that might be logged). Fail-open like the keyless adapters.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from clav.clock import Clock
from clav.common.logging import get_logger
from clav.domain.models import NewsItem
from clav.integrations.news.http import HttpTextFetcher, TextFetcher
from clav.interfaces.news import NewsSource

_logger = get_logger(__name__)

NEWSAPI_URL_TEMPLATE = (
    "https://newsapi.org/v2/everything?q={symbol}&language=en&sortBy=publishedAt&pageSize=20"
)


def _parse_published(raw: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime(1970, 1, 1, tzinfo=UTC)
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


class NewsApiSource(NewsSource):
    def __init__(
        self,
        *,
        clock: Clock,
        api_key: str | None = None,
        url_template: str = NEWSAPI_URL_TEMPLATE,
        fetcher: TextFetcher | None = None,
    ) -> None:
        self._clock = clock
        self._api_key = api_key
        self._url_template = url_template
        self._fetcher = fetcher or HttpTextFetcher()

    @property
    def is_active(self) -> bool:
        return bool(self._api_key)

    def fetch(self, symbol: str, since: datetime) -> list[NewsItem]:
        if not self.is_active:
            return []
        url = self._url_template.format(symbol=symbol.upper())
        try:
            body = self._fetcher.get(url, headers={"X-Api-Key": self._api_key or ""})
            payload = json.loads(body)
        except Exception as exc:  # fail-open: never abort the cycle
            _logger.warning("newsapi_fetch_failed", symbol=symbol, error=str(exc))
            return []

        fetched_at = self._clock.now()
        items: list[NewsItem] = []
        for article in payload.get("articles", []):
            headline = (article.get("title") or "").strip()
            if not headline:
                continue
            published_at = _parse_published(article.get("publishedAt", ""))
            if published_at < since:
                continue
            source = (article.get("source") or {}).get("name") or "newsapi"
            items.append(
                NewsItem(
                    id=article.get("url") or headline,
                    symbol=symbol.upper(),
                    headline=headline,
                    body=(article.get("description") or article.get("content") or "").strip(),
                    url=article.get("url"),
                    source=f"newsapi:{source}",
                    published_at=published_at,
                    fetched_at=fetched_at,
                )
            )
        return items
