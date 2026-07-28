"""Fail-open sources report *why* they returned nothing.

The adapters swallow exceptions and return ``[]`` so a dead source can never
abort a cycle. The cost is that "blocked" and "nothing to report" are the same
empty list everywhere downstream. These tests pin the distinction: a 403 must
surface as critical, and an genuinely quiet source must not.
"""

from __future__ import annotations

from datetime import UTC, datetime

from clav.clock import FakeClock
from clav.integrations.discovery import ApeWisdomSource, StockTwitsTrendingSource
from clav.integrations.news import EdgarNewsSource, RSSNewsSource
from clav.integrations.social import RedditSource, StockTwitsSource
from clav.web.health_view import _source_tiles

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


class _Fetcher:
    def __init__(self, body: str | Exception) -> None:
        self._body = body

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> str:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _blocked() -> _Fetcher:
    return _Fetcher(RuntimeError("403 Forbidden"))


# --- the distinction an empty list destroys ---------------------------------


def test_blocked_source_is_critical_with_the_real_reason() -> None:
    source = ApeWisdomSource(fetcher=_blocked())
    assert source.fetch() == []

    outcome = source.last_outcome
    assert outcome is not None
    assert outcome.ok is False
    assert outcome.status == "critical"
    assert "403" in (outcome.error or "")


def test_quiet_source_is_ok_not_critical() -> None:
    """A successful fetch that happens to return nothing is normal, and must
    never render as a fault -- that would train the operator to ignore it."""
    source = ApeWisdomSource(fetcher=_Fetcher('{"results": []}'))
    assert source.fetch() == []

    outcome = source.last_outcome
    assert outcome is not None
    assert outcome.ok is True
    assert outcome.status == "ok"
    assert outcome.item_count == 0


def test_never_run_source_reports_nothing_rather_than_health() -> None:
    """An adapter that hasn't fetched this process must not claim to be fine."""
    assert ApeWisdomSource(fetcher=_blocked()).last_outcome is None


# --- every fail-open adapter participates ------------------------------------


def test_all_social_and_news_adapters_record_a_block() -> None:
    clock = FakeClock(NOW)
    sources = [
        RedditSource(clock=clock, subreddits=("stocks",), fetcher=_blocked()),
        StockTwitsSource(clock=clock, fetcher=_blocked()),
        RSSNewsSource(clock=clock, fetcher=_blocked()),
        EdgarNewsSource(clock=clock, fetcher=_blocked()),
    ]
    for source in sources:
        assert source.fetch("NVDA", NOW) == []
        outcome = source.last_outcome
        assert outcome is not None, f"{type(source).__name__} recorded nothing"
        assert outcome.ok is False, f"{type(source).__name__} claimed success while blocked"
        assert outcome.name, "outcome needs a human-readable source name"


def test_all_discovery_adapters_record_a_block() -> None:
    for source in (
        ApeWisdomSource(fetcher=_blocked()),
        StockTwitsTrendingSource(fetcher=_blocked()),
    ):
        assert source.fetch() == []
        assert source.last_outcome is not None
        assert source.last_outcome.ok is False


def test_reddit_partial_failure_counts_as_down() -> None:
    """One subreddit 403ing means the source is effectively blocked, even though
    the others may return -- Reddit blocks per-IP, not per-subreddit."""
    source = RedditSource(clock=FakeClock(NOW), subreddits=("a", "b"), fetcher=_blocked())
    source.fetch("NVDA", NOW)
    assert source.last_outcome is not None
    assert source.last_outcome.ok is False


# --- what the operator actually sees -----------------------------------------


def test_tiles_show_the_error_on_failure_and_the_count_on_success() -> None:
    external = {
        "alpaca": {"status": "ok", "value": {"ok": True}},
        "source:reddit": {
            "status": "critical",
            "value": {"ok": False, "items": 0, "error": "RuntimeError: 403 Forbidden"},
        },
        "source:stocktwits": {"status": "ok", "value": {"ok": True, "items": 30}},
    }
    tiles = {t["name"]: t for t in _source_tiles(external)}

    assert set(tiles) == {"src reddit", "src stocktwits"}, "only source:* entries become tiles"
    assert tiles["src reddit"]["status"] == "critical"
    assert "403" in tiles["src reddit"]["display"]
    assert tiles["src stocktwits"]["display"] == "30 items"


def test_no_source_events_yields_no_tiles() -> None:
    """A pre-existing install with no source health recorded shows nothing
    rather than a row of 'unknown' noise."""
    assert _source_tiles({"alpaca": {"status": "ok", "value": {}}}) == []
