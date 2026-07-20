"""Story 3.3 — news/social dedup, TTL cache, staleness cutoff, retention."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from clav.clock import FakeClock
from clav.common.cache import TtlCache
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import Engagement, NewsItem, SocialDigest, SocialItem

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _news(headline: str, source: str, published_at: datetime = NOW) -> NewsItem:
    return NewsItem(
        id=f"{source}-{headline}",
        symbol="AAPL",
        headline=headline,
        body="body",
        url=f"https://example.com/{source}",
        source=source,
        published_at=published_at,
        fetched_at=NOW,
    )


def test_same_article_from_two_adapters_deduped(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        inst = repos.instruments.get_or_create("AAPL")
        # Same headline, two different sources -> identical content_hash.
        rss = _news("Apple beats earnings", "rss")
        edgar = _news("apple  beats   earnings", "edgar")
        inserted = repos.news_items.add_many(inst.id, [rss, edgar])
        assert len(inserted) == 1  # second collapses on content_hash

        # A later cycle re-fetching the same story inserts nothing new.
        again = repos.news_items.add_many(inst.id, [_news("Apple beats earnings", "newsapi")])
        assert again == []


def test_staleness_cutoff_excludes_old_items(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        inst = repos.instruments.get_or_create("AAPL")
        fresh = _news("fresh news", "rss", published_at=NOW - timedelta(hours=1))
        stale = _news("ancient news", "rss", published_at=NOW - timedelta(hours=200))
        repos.news_items.add_many(inst.id, [fresh, stale])

        result = repos.news_items.get_for_analysis(
            inst.id, now=NOW, max_age_hours=72, limit=50
        )
        headlines = [i.headline for i in result]
        assert "fresh news" in headlines
        assert "ancient news" not in headlines


def test_news_retention_keeps_last_k(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        inst = repos.instruments.get_or_create("AAPL")
        for i in range(10):
            repos.news_items.add_many(
                inst.id, [_news(f"headline {i}", "rss", published_at=NOW - timedelta(hours=i))]
            )
        deleted = repos.news_items.prune(inst.id, keep=3)
        assert deleted == 7
        remaining = repos.news_items.get_for_analysis(
            inst.id, now=NOW, max_age_hours=1000, limit=50
        )
        assert len(remaining) == 3
        # newest three kept
        assert {i.headline for i in remaining} == {"headline 0", "headline 1", "headline 2"}


def test_ttl_cache_hit_and_miss() -> None:
    clock = FakeClock(NOW)
    cache = TtlCache(clock=clock, ttl_seconds=3600)
    key = "rss:AAPL"
    assert cache.should_fetch(key) is True  # never fetched
    cache.mark(key)
    assert cache.should_fetch(key) is False  # within TTL
    clock.set(NOW + timedelta(seconds=3600))
    assert cache.should_fetch(key) is True  # TTL elapsed


def _digest(mention_volume: int, generated_at: datetime) -> SocialDigest:
    return SocialDigest(
        symbol="AAPL",
        qualifying_post_count=mention_volume,
        bull_count=mention_volume,
        bear_count=0,
        bull_bear_ratio=float(mention_volume + 1),
        mention_volume=mention_volume,
        baseline_volume=1.0,
        volume_ratio=1.0,
        anomaly_flag=False,
        top_posts=[
            SocialItem(
                symbol="AAPL",
                text="buy AAPL",
                author="alice",
                author_reputation=100.0,
                engagement=Engagement(score=10, replies=2),
                posted_at=generated_at,
                source="reddit:stocks",
                sentiment="bull",
            )
        ],
        generated_at=generated_at,
    )


def test_social_digest_roundtrip_and_baseline(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        inst = repos.instruments.get_or_create("AAPL")
        repos.social_digests.add(inst.id, _digest(4, NOW - timedelta(days=2)))
        repos.social_digests.add(inst.id, _digest(6, NOW - timedelta(days=1)))
        repos.social_digests.add(inst.id, _digest(8, NOW))

        latest = repos.social_digests.latest(inst.id)
        assert latest is not None
        assert latest.mention_volume == 8
        assert latest.symbol == "AAPL"
        assert latest.top_posts[0].sentiment == "bull"
        assert latest.top_posts[0].engagement.score == 10

        baseline = repos.social_digests.rolling_baseline(inst.id, window=20)
        assert baseline == (4 + 6 + 8) / 3


def test_social_digest_baseline_empty_history_is_zero(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        inst = repos.instruments.get_or_create("AAPL")
        assert repos.social_digests.rolling_baseline(inst.id, window=20) == 0.0
        assert repos.social_digests.latest(inst.id) is None
