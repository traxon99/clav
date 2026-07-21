"""Story 3.1 — news/filings adapters over recorded fixtures (no live network)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from clav.clock import FakeClock
from clav.domain.models import NewsItem
from clav.integrations.news import EdgarNewsSource, NewsApiSource, RSSNewsSource
from clav.integrations.news.feed import FeedParseError, parse_feed

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
SINCE = datetime(2026, 7, 1, tzinfo=UTC)


class FakeFetcher:
    """Injected in place of the httpx fetcher — returns a canned body or raises."""

    def __init__(self, body: str | None = None, error: Exception | None = None) -> None:
        self._body = body
        self._error = error
        self.calls: list[str] = []

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> str:
        self.calls.append(url)
        self.last_headers = headers
        if self._error is not None:
            raise self._error
        assert self._body is not None
        return self._body


RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>AAPL headlines</title>
    <item>
      <title>Apple beats earnings expectations</title>
      <description>&lt;p&gt;Strong iPhone sales&lt;/p&gt;</description>
      <link>https://example.com/aapl-earnings</link>
      <guid>guid-aapl-1</guid>
      <pubDate>Wed, 15 Jul 2026 13:30:00 GMT</pubDate>
    </item>
    <item>
      <title>Old stale item</title>
      <description>from before the window</description>
      <link>https://example.com/old</link>
      <guid>guid-old</guid>
      <pubDate>Mon, 02 Jun 2026 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

EDGAR_FIXTURE = """<?xml version="1.0" encoding="ISO-8859-1"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Filings for AAPL</title>
  <entry>
    <title>8-K - Current report</title>
    <link rel="alternate" href="https://www.sec.gov/edgar/aapl-8k"/>
    <summary type="html">Material event</summary>
    <updated>2026-07-14T16:00:00-04:00</updated>
    <id>accession-8k-1</id>
  </entry>
  <entry>
    <title>SC 13G - Beneficial ownership</title>
    <link rel="alternate" href="https://www.sec.gov/edgar/aapl-13g"/>
    <summary>Ownership filing</summary>
    <updated>2026-07-16T10:00:00-04:00</updated>
    <id>accession-13g</id>
  </entry>
  <entry>
    <title>10-Q - Quarterly report</title>
    <link rel="alternate" href="https://www.sec.gov/edgar/aapl-10q"/>
    <summary>Quarterly numbers</summary>
    <updated>2026-06-20T09:00:00-04:00</updated>
    <id>accession-10q</id>
  </entry>
</feed>
"""

NEWSAPI_FIXTURE = """{
  "status": "ok",
  "totalResults": 1,
  "articles": [
    {
      "source": {"id": null, "name": "Reuters"},
      "author": "Jane",
      "title": "Apple unveils new product line",
      "description": "A big reveal",
      "url": "https://example.com/newsapi-aapl",
      "publishedAt": "2026-07-18T08:00:00Z",
      "content": "full text"
    },
    {
      "source": {"name": "Old"},
      "title": "Way too old",
      "url": "https://example.com/too-old",
      "publishedAt": "2026-06-01T08:00:00Z"
    }
  ]
}"""


def test_rss_parses_and_filters_by_since() -> None:
    fetcher = FakeFetcher(RSS_FIXTURE)
    src = RSSNewsSource(clock=FakeClock(NOW), fetcher=fetcher, source_name="rss")
    items = src.fetch("aapl", SINCE)

    assert len(items) == 1
    item = items[0]
    assert isinstance(item, NewsItem)
    assert item.symbol == "AAPL"
    assert item.headline == "Apple beats earnings expectations"
    assert item.body == "Strong iPhone sales"  # HTML stripped
    assert item.url == "https://example.com/aapl-earnings"
    assert item.source == "rss"
    assert item.fetched_at == NOW
    assert item.published_at.tzinfo is not None
    assert "AAPL" in fetcher.calls[0]


def test_rss_failure_degrades_to_empty() -> None:
    src = RSSNewsSource(
        clock=FakeClock(NOW), fetcher=FakeFetcher(error=RuntimeError("boom"))
    )
    assert src.fetch("AAPL", SINCE) == []


def test_rss_malformed_feed_degrades_to_empty() -> None:
    src = RSSNewsSource(clock=FakeClock(NOW), fetcher=FakeFetcher("<not xml"))
    assert src.fetch("AAPL", SINCE) == []


def test_rss_template_requires_symbol_placeholder() -> None:
    with pytest.raises(ValueError, match="symbol"):
        RSSNewsSource(clock=FakeClock(NOW), feed_template="https://example.com/feed")


def test_edgar_keeps_only_material_filing_types() -> None:
    fetcher = FakeFetcher(EDGAR_FIXTURE)
    src = EdgarNewsSource(clock=FakeClock(NOW), fetcher=fetcher)
    items = src.fetch("AAPL", datetime(2026, 6, 1, tzinfo=UTC))

    headlines = [i.headline for i in items]
    assert any("8-K" in h for h in headlines)
    assert any("10-Q" in h for h in headlines)
    # SC 13G is not in the default material-types set.
    assert not any("13G" in h for h in headlines)
    assert all(i.source == "edgar" for i in items)


def test_edgar_respects_since_window() -> None:
    src = EdgarNewsSource(clock=FakeClock(NOW), fetcher=FakeFetcher(EDGAR_FIXTURE))
    items = src.fetch("AAPL", datetime(2026, 7, 1, tzinfo=UTC))
    # 10-Q is dated 2026-06-20, before the window → excluded.
    assert all("10-Q" not in i.headline for i in items)


def test_edgar_failure_degrades_to_empty() -> None:
    src = EdgarNewsSource(clock=FakeClock(NOW), fetcher=FakeFetcher(error=RuntimeError("x")))
    assert src.fetch("AAPL", SINCE) == []


def test_newsapi_inactive_without_key_returns_empty() -> None:
    fetcher = FakeFetcher(NEWSAPI_FIXTURE)
    src = NewsApiSource(clock=FakeClock(NOW), api_key=None, fetcher=fetcher)
    assert src.is_active is False
    assert src.fetch("AAPL", SINCE) == []
    assert fetcher.calls == []  # no network attempted


def test_newsapi_active_with_key_parses_and_filters() -> None:
    fetcher = FakeFetcher(NEWSAPI_FIXTURE)
    src = NewsApiSource(clock=FakeClock(NOW), api_key="secret", fetcher=fetcher)
    assert src.is_active is True
    items = src.fetch("AAPL", SINCE)

    assert len(items) == 1
    assert items[0].headline == "Apple unveils new product line"
    assert items[0].source == "newsapi:Reuters"
    assert fetcher.last_headers == {"X-Api-Key": "secret"}


def test_content_hash_dedups_same_headline_across_sources() -> None:
    a = NewsItem(
        id="1",
        symbol="AAPL",
        headline="Apple  Beats   Earnings",
        source="rss",
        published_at=NOW,
        fetched_at=NOW,
    )
    b = NewsItem(
        id="2",
        symbol="aapl",
        headline="apple beats earnings",
        source="edgar",
        published_at=NOW,
        fetched_at=NOW,
    )
    assert a.content_hash == b.content_hash


def test_parse_feed_raises_on_garbage() -> None:
    with pytest.raises(FeedParseError):
        parse_feed("<<<not-a-feed")
