"""EdgarNewsSource — SEC EDGAR filings as hard catalysts (free, keyless, Story 3.1).

Reads the per-company filings **Atom** feed from EDGAR's ``browse-edgar`` endpoint
and keeps only the material filing types (8-K, 10-Q, 10-K, Form-4 by default). SEC
policy requires a descriptive ``User-Agent`` — supplied by the shared fetcher.
Fail-open: any fetch/parse error degrades to an empty list.
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

EDGAR_FEED_TEMPLATE = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&ticker={symbol}"
    "&type=&dateb=&owner=include&count=40&output=atom"
)

DEFAULT_FILING_TYPES = ("8-K", "10-Q", "10-K", "4")

_TAG_RE = re.compile(r"<[^>]+>")
# EDGAR Atom titles look like "8-K - Current report" or "10-K/A - ...".
_TYPE_RE = re.compile(r"^\s*([0-9A-Z]+(?:-[0-9A-Z]+)?(?:/A)?)\b")


def _strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _filing_type(title: str) -> str | None:
    match = _TYPE_RE.match(title.strip())
    return match.group(1) if match else None


class EdgarNewsSource(NewsSource):
    def __init__(
        self,
        *,
        clock: Clock,
        filing_types: tuple[str, ...] = DEFAULT_FILING_TYPES,
        feed_template: str = EDGAR_FEED_TEMPLATE,
        fetcher: TextFetcher | None = None,
    ) -> None:
        self._clock = clock
        # Normalize (drop any amendment suffix) for prefix matching.
        self._filing_types = tuple(t.upper() for t in filing_types)
        self._feed_template = feed_template
        self._fetcher = fetcher or HttpTextFetcher()

    def _wanted(self, filing_type: str | None) -> bool:
        if filing_type is None:
            return False
        base = filing_type.upper().split("/", 1)[0]
        return base in self._filing_types

    def fetch(self, symbol: str, since: datetime) -> list[NewsItem]:
        url = self._feed_template.format(symbol=symbol.upper())
        try:
            body = self._fetcher.get(url)
            entries = parse_feed(body)
        except Exception as exc:  # fail-open: never abort the cycle on a bad feed
            _logger.warning("edgar_fetch_failed", symbol=symbol, error=str(exc))
            return []

        fetched_at = self._clock.now()
        items: list[NewsItem] = []
        for entry in entries:
            title = _strip_html(entry.title)
            filing_type = _filing_type(title)
            if not self._wanted(filing_type):
                continue
            if entry.published_at < since:
                continue
            items.append(
                NewsItem(
                    id=entry.id or entry.link or title,
                    symbol=symbol.upper(),
                    headline=f"SEC {filing_type}: {title}",
                    body=_strip_html(entry.summary),
                    url=entry.link,
                    source="edgar",
                    published_at=entry.published_at,
                    fetched_at=fetched_at,
                )
            )
        return items
