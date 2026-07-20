"""RSSNewsSource — per-symbol RSS/Atom headline feeds (free, keyless, Story 3.1).

The feed URL is a configurable template containing ``{symbol}`` (default: Yahoo
Finance's per-ticker headline feed). Fail-open: any fetch/parse error degrades to
an empty list and is logged, never raised into the scan cycle.
"""

from __future__ import annotations

import html
import re
from datetime import datetime

from clav.clock import Clock
from clav.common.logging import get_logger
from clav.domain.models import NewsItem
from clav.integrations.news.feed import parse_feed
from clav.integrations.news.http import HttpTextFetcher, TextFetcher
from clav.interfaces.news import NewsSource

_logger = get_logger(__name__)

DEFAULT_FEED_TEMPLATE = (
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
)

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text)).strip()


class RSSNewsSource(NewsSource):
    def __init__(
        self,
        *,
        clock: Clock,
        feed_template: str = DEFAULT_FEED_TEMPLATE,
        source_name: str = "rss",
        fetcher: TextFetcher | None = None,
    ) -> None:
        if "{symbol}" not in feed_template:
            raise ValueError("feed_template must contain a '{symbol}' placeholder")
        self._clock = clock
        self._feed_template = feed_template
        self._source_name = source_name
        self._fetcher = fetcher or HttpTextFetcher()

    def fetch(self, symbol: str, since: datetime) -> list[NewsItem]:
        url = self._feed_template.format(symbol=symbol.upper())
        try:
            body = self._fetcher.get(url)
            entries = parse_feed(body)
        except Exception as exc:  # fail-open: never abort the cycle on a bad feed
            _logger.warning(
                "rss_fetch_failed", symbol=symbol, source=self._source_name, error=str(exc)
            )
            return []

        fetched_at = self._clock.now()
        items: list[NewsItem] = []
        for entry in entries:
            if entry.published_at < since:
                continue
            headline = _strip_html(entry.title)
            if not headline:
                continue
            items.append(
                NewsItem(
                    id=entry.id or entry.link or headline,
                    symbol=symbol.upper(),
                    headline=headline,
                    body=_strip_html(entry.summary),
                    url=entry.link,
                    source=self._source_name,
                    published_at=entry.published_at,
                    fetched_at=fetched_at,
                )
            )
        return items
