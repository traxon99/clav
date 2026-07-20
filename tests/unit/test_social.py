"""Story 3.2 — deterministic Stage-1 social filtering + aggregation (domain)."""

from __future__ import annotations

from datetime import UTC, datetime

from clav.domain.models import Engagement, SocialItem
from clav.domain.social import (
    SocialFilterParams,
    build_digest,
    classify_sentiment,
    collapse_near_duplicates,
    passes_stage1,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
PARAMS = SocialFilterParams()


def _item(
    text: str = "AAPL looks like a great buy, going long",
    *,
    score: int = 100,
    replies: int = 10,
    reputation: float = 500.0,
    author: str = "alice",
    posted_at: datetime = NOW,
    sentiment: str | None = None,
) -> SocialItem:
    return SocialItem(
        symbol="AAPL",
        text=text,
        author=author,
        author_reputation=reputation,
        engagement=Engagement(score=score, replies=replies),
        posted_at=posted_at,
        source="reddit:test",
        sentiment=sentiment,  # type: ignore[arg-type]
    )


def test_engagement_floor_drops_low_score() -> None:
    assert passes_stage1(_item(score=100), PARAMS) is True
    assert passes_stage1(_item(score=1), PARAMS) is False


def test_reputation_floor_drops_throwaway_accounts() -> None:
    assert passes_stage1(_item(reputation=500), PARAMS) is True
    assert passes_stage1(_item(reputation=0), PARAMS) is False


def test_cashtag_stuffing_dropped() -> None:
    stuffed = _item(text="$AAPL $TSLA $NVDA $AMD $MSFT $GOOG $META buy all")
    assert passes_stage1(stuffed, PARAMS) is False


def test_promo_keyword_and_link_dropped() -> None:
    assert passes_stage1(_item(text="guaranteed 100x join my discord.gg/xyz"), PARAMS) is False
    assert passes_stage1(_item(text="free alert dm me now"), PARAMS) is False


def test_near_duplicate_copypasta_collapses() -> None:
    posts = [
        _item(text="AAPL to the moon buy now", author=f"bot{i}", score=10 + i)
        for i in range(200)
    ]
    collapsed = collapse_near_duplicates(posts)
    assert len(collapsed) == 1
    # keeps the highest-engagement representative
    assert collapsed[0].engagement.score == 209


def test_classify_sentiment_lexicon_and_explicit() -> None:
    assert classify_sentiment(_item(text="strong buy, bullish breakout, calls")) == "bull"
    assert classify_sentiment(_item(text="short this, bearish dump incoming, puts")) == "bear"
    assert classify_sentiment(_item(text="the company exists")) == "neutral"
    # explicit label wins over lexicon
    assert classify_sentiment(_item(text="bearish words dump", sentiment="bull")) == "bull"


def test_digest_bull_bear_and_top_sample() -> None:
    items = [
        _item(text="buy bullish calls moon", score=90, author="a"),
        _item(text="buy bullish long", score=80, author="b"),
        _item(text="sell bearish puts dump", score=70, author="c"),
    ]
    digest = build_digest(
        "AAPL", items, baseline_volume=3.0, params=PARAMS, now=NOW
    )
    assert digest.qualifying_post_count == 3
    assert digest.bull_count == 2
    assert digest.bear_count == 1
    assert digest.bull_bear_ratio == (2 + 1) / (1 + 1)
    assert digest.top_posts[0].engagement.score == 90
    assert digest.anomaly_flag is False


def test_digest_anomaly_flag_on_volume_spike() -> None:
    items = [_item(author=f"u{i}", text=f"AAPL update number {i} buy") for i in range(12)]
    digest = build_digest(
        "AAPL", items, baseline_volume=2.0, params=PARAMS, now=NOW
    )
    # 12 qualifying vs baseline 2.0 => ratio 6.0 >= 3.0 multiplier, >= 5 posts
    assert digest.volume_ratio == 6.0
    assert digest.anomaly_flag is True


def test_low_liquidity_lowers_anomaly_bar() -> None:
    # 5 qualifying posts vs baseline 2.0 => volume_ratio 2.5: below the strict
    # 3.0 multiplier (normal) but at/above the 2.0 low-liquidity multiplier.
    items2 = [_item(author=f"u{i}", text=f"THINLY traded {i} buy") for i in range(5)]
    normal = build_digest(
        "XYZ", items2, baseline_volume=2.0, params=PARAMS, now=NOW, is_low_liquidity=False
    )
    low_liq = build_digest(
        "XYZ", items2, baseline_volume=2.0, params=PARAMS, now=NOW, is_low_liquidity=True
    )
    assert normal.volume_ratio == 2.5
    assert normal.anomaly_flag is False  # 2.5 < 3.0 strict multiplier
    assert low_liq.anomaly_flag is True  # 2.5 >= 2.0 low-liquidity multiplier


def test_all_junk_input_yields_empty_digest() -> None:
    junk = [_item(score=0, reputation=0) for _ in range(10)]
    digest = build_digest("AAPL", junk, baseline_volume=5.0, params=PARAMS, now=NOW)
    assert digest.is_empty is True
    assert digest.qualifying_post_count == 0
    assert digest.anomaly_flag is False


def test_single_high_karma_bot_cannot_move_aggregate() -> None:
    # One reputable account posting the same thing many times collapses to one.
    spam = [
        _item(text="AAPL guaranteed moon", author="whale", score=1000, reputation=100000)
        for _ in range(50)
    ]
    # 'guaranteed' is a promo keyword -> all dropped anyway.
    digest = build_digest("AAPL", spam, baseline_volume=1.0, params=PARAMS, now=NOW)
    assert digest.qualifying_post_count == 0
