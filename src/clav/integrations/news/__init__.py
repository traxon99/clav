"""Free-tier news + filings adapters behind the ``NewsSource`` interface (Story 3.1).

- ``RSSNewsSource`` — per-symbol RSS/Atom headline feeds (keyless).
- ``EdgarNewsSource`` — SEC EDGAR filings Atom feed (keyless, declared User-Agent).
- ``NewsApiSource`` — optional, only active when an API key is configured (off by
  default; absence of a key is not an error — see epic decision #5).

No vendor SDKs: everything is ``httpx`` + stdlib XML/JSON parsing, so nothing here
leaks into ``domain``/``interfaces`` (import-linter contract stays green).
"""

from __future__ import annotations

from clav.integrations.news.edgar import EdgarNewsSource
from clav.integrations.news.http import HttpTextFetcher, TextFetcher
from clav.integrations.news.newsapi import NewsApiSource
from clav.integrations.news.rss import RSSNewsSource

__all__ = [
    "EdgarNewsSource",
    "HttpTextFetcher",
    "NewsApiSource",
    "RSSNewsSource",
    "TextFetcher",
]
