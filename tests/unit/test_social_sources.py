"""Story 3.2 — Reddit/StockTwits adapters over recorded fixtures (no live network)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from clav.clock import FakeClock
from clav.domain.models import SocialItem
from clav.integrations.social import RedditSource, StockTwitsSource

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
SINCE = datetime(2026, 7, 1, tzinfo=UTC)
JUL19 = int(datetime(2026, 7, 19, 12, 0, tzinfo=UTC).timestamp())
JUN01 = int(datetime(2026, 6, 1, 12, 0, tzinfo=UTC).timestamp())


class FakeFetcher:
    def __init__(self, body: str | None = None, error: Exception | None = None) -> None:
        self._body = body
        self._error = error
        self.calls: list[str] = []

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> str:
        self.calls.append(url)
        if self._error is not None:
            raise self._error
        assert self._body is not None
        return self._body


REDDIT_FIXTURE = json.dumps(
    {
        "data": {
            "children": [
                {
                    "data": {
                        "title": "AAPL earnings look strong",
                        "selftext": "loading up on calls",
                        "author": "trader_joe",
                        "author_karma": 4200,
                        "score": 350,
                        "num_comments": 88,
                        "created_utc": JUL19,
                    }
                },
                {
                    "data": {
                        "title": "old post",
                        "selftext": "",
                        "author": "someone",
                        "score": 10,
                        "num_comments": 1,
                        "created_utc": JUN01,
                    }
                },
            ]
        }
    }
)

STOCKTWITS_FIXTURE = """{
  "messages": [
    {
      "id": 1,
      "body": "$AAPL bullish into earnings",
      "created_at": "2026-07-19T12:00:00Z",
      "user": {"username": "st_user", "followers": 1500},
      "entities": {"sentiment": {"basic": "Bullish"}},
      "likes": {"total": 42}
    },
    {
      "id": 2,
      "body": "$AAPL no opinion",
      "created_at": "2026-07-19T13:00:00Z",
      "user": {"username": "st_two", "followers": 3},
      "entities": {"sentiment": null},
      "likes": {"total": 0}
    }
  ]
}"""


def test_reddit_normalizes_and_filters_by_since() -> None:
    fetcher = FakeFetcher(REDDIT_FIXTURE)
    src = RedditSource(clock=FakeClock(NOW), subreddits=("wallstreetbets",), fetcher=fetcher)
    items = src.fetch("aapl", SINCE)

    assert len(items) == 1
    item = items[0]
    assert isinstance(item, SocialItem)
    assert item.symbol == "AAPL"
    assert "calls" in item.text
    assert item.author == "trader_joe"
    assert item.author_reputation == 4200.0
    assert item.engagement.score == 350
    assert item.engagement.replies == 88
    assert item.source == "reddit:wallstreetbets"
    assert "%24AAPL" in fetcher.calls[0]


def test_reddit_defaults_reputation_when_missing() -> None:
    body = json.dumps(
        {
            "data": {
                "children": [
                    {
                        "data": {
                            "title": "AAPL",
                            "selftext": "buy",
                            "author": "x",
                            "score": 20,
                            "num_comments": 2,
                            "created_utc": JUL19,
                        }
                    }
                ]
            }
        }
    )
    src = RedditSource(
        clock=FakeClock(NOW),
        subreddits=("stocks",),
        default_author_reputation=1234.0,
        fetcher=FakeFetcher(body),
    )
    items = src.fetch("AAPL", SINCE)
    assert items[0].author_reputation == 1234.0


def test_reddit_failure_degrades_to_empty() -> None:
    src = RedditSource(
        clock=FakeClock(NOW), subreddits=("stocks",), fetcher=FakeFetcher(error=RuntimeError("x"))
    )
    assert src.fetch("AAPL", SINCE) == []


def test_stocktwits_extracts_explicit_sentiment_and_reputation() -> None:
    src = StockTwitsSource(clock=FakeClock(NOW), fetcher=FakeFetcher(STOCKTWITS_FIXTURE))
    items = src.fetch("AAPL", SINCE)

    assert len(items) == 2
    bullish = items[0]
    assert bullish.sentiment == "bull"
    assert bullish.author_reputation == 1500.0
    assert bullish.engagement.score == 42
    assert items[1].sentiment is None  # no explicit label -> deterministic later


def test_stocktwits_failure_degrades_to_empty() -> None:
    src = StockTwitsSource(clock=FakeClock(NOW), fetcher=FakeFetcher(error=RuntimeError("x")))
    assert src.fetch("AAPL", SINCE) == []


def test_stocktwits_malformed_json_degrades_to_empty() -> None:
    src = StockTwitsSource(clock=FakeClock(NOW), fetcher=FakeFetcher("{not json"))
    assert src.fetch("AAPL", SINCE) == []
