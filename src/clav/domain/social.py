"""Deterministic Stage-1 social filtering + aggregation (Story 3.2).

Pure/DB-free/vendor-free domain logic (mirrors ``domain/risk``): the social
adapters normalize raw posts into ``SocialItem``s, then hand them here to be
filtered for obvious bot/spam junk and collapsed into a compact ``SocialDigest``.
Only the digest — never the raw firehose — reaches Gemini (Story 3.4), so a
single bot can't move the signal and the token cost stays bounded.

Thresholds arrive as a plain frozen ``SocialFilterParams`` (the composition root
translates ``SocialConfig`` into it, keeping ``clav.config`` out of ``domain``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from clav.domain.models import SocialDigest, SocialItem, SocialSentiment

# A graded text scorer returning [-1, +1]. Passed in structurally (never imported)
# so ``domain`` stays vendor-free and free of an import cycle through
# ``interfaces``; the composition root supplies
# ``clav.integrations.sentiment.LexiconScorer(...).score``.
ScoreText = Callable[[str], float]

_CASHTAG_RE = re.compile(r"\$[A-Za-z]{1,6}\b")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# Fallback sentiment lexicon, used only when no scorer is injected. This is
# intentionally crude — it cannot see negation, intensity, or emphasis, which is
# precisely why ``ScoreText`` exists. Kept so ``domain`` remains runnable with
# zero third-party packages.
_BULL_WORDS = frozenset(
    {
        "buy",
        "buying",
        "bought",
        "bull",
        "bullish",
        "long",
        "calls",
        "call",
        "moon",
        "rocket",
        "squeeze",
        "breakout",
        "rally",
        "undervalued",
        "up",
        "green",
        "rip",
        "surge",
        "beat",
        "beats",
        "upgrade",
        "accumulate",
    }
)
_BEAR_WORDS = frozenset(
    {
        "sell",
        "selling",
        "sold",
        "bear",
        "bearish",
        "short",
        "puts",
        "put",
        "dump",
        "crash",
        "drop",
        "tank",
        "collapse",
        "overvalued",
        "down",
        "red",
        "miss",
        "misses",
        "downgrade",
        "avoid",
        "bagholder",
    }
)
_WORD_RE = re.compile(r"[a-z']+")


@dataclass(frozen=True)
class SocialFilterParams:
    # See clav.config.SocialConfig for why these aren't (5, 50.0) -- that
    # pair was calibrated for Reddit-scale engagement and filters out the
    # vast majority of genuine StockTwits posts.
    min_engagement_score: int = 1
    min_replies: int = 0
    min_author_reputation: float = 10.0
    max_symbols_per_post: int = 5
    promo_keywords: tuple[str, ...] = (
        "pump",
        "guaranteed",
        "to the moon",
        "join my",
        "free alert",
        "dm me",
        "sign up",
        "not financial advice but",
        "100x",
        "1000%",
    )
    promo_link_domains: tuple[str, ...] = (
        "discord.gg",
        "discord.com",
        "t.me",
        "telegram",
        "bit.ly",
        "patreon",
        "substack",
        "onlyfans",
        "linktr.ee",
    )
    near_dup_enabled: bool = True
    top_n: int = 5
    anomaly_volume_multiplier: float = 3.0
    low_liquidity_volume_multiplier: float = 2.0
    min_posts_for_anomaly: int = 5
    # Dead-band around zero within which a graded score counts as neutral rather
    # than bull/bear. 0.05 is VADER's documented convention and keeps the tally
    # roughly as sensitive as the old word-count it replaces.
    sentiment_neutral_band: float = 0.05


def classify_sentiment(
    item: SocialItem,
    score_text: ScoreText | None = None,
    neutral_band: float = 0.05,
) -> SocialSentiment:
    """Explicit source label wins (StockTwits Bullish/Bearish); otherwise band the
    injected graded score, falling back to the crude word tally when none is given."""
    if item.sentiment is not None:
        return item.sentiment
    if score_text is not None:
        return band_sentiment(score_text(item.text), neutral_band)
    words = _WORD_RE.findall(item.text.lower())
    bull = sum(1 for w in words if w in _BULL_WORDS)
    bear = sum(1 for w in words if w in _BEAR_WORDS)
    if bull > bear:
        return "bull"
    if bear > bull:
        return "bear"
    return "neutral"


def band_sentiment(score: float, neutral_band: float = 0.05) -> SocialSentiment:
    """Collapse a graded [-1, +1] score into the bull/bear/neutral tally label."""
    if score > neutral_band:
        return "bull"
    if score < -neutral_band:
        return "bear"
    return "neutral"


def _is_promo(text: str, params: SocialFilterParams) -> bool:
    lowered = text.lower()
    if any(kw in lowered for kw in params.promo_keywords):
        return True
    for url in _URL_RE.findall(lowered):
        if any(domain in url for domain in params.promo_link_domains):
            return True
    return False


def passes_stage1(item: SocialItem, params: SocialFilterParams) -> bool:
    """The cheap, un-arguable filters: engagement + reputation floors,
    cashtag-stuffing cap, and promo/link/keyword rejection."""
    if item.engagement.score < params.min_engagement_score:
        return False
    if item.engagement.replies < params.min_replies:
        return False
    if item.author_reputation < params.min_author_reputation:
        return False
    if len(_CASHTAG_RE.findall(item.text)) > params.max_symbols_per_post:
        return False
    return not _is_promo(item.text, params)


def collapse_near_duplicates(items: list[SocialItem]) -> list[SocialItem]:
    """Coordinated copypasta across accounts shares a ``dedup_key``; keep the
    single highest-engagement representative of each cluster. This is what stops
    200 near-identical posts from being counted 200 times."""
    best: dict[str, SocialItem] = {}
    for item in items:
        key = item.dedup_key
        current = best.get(key)
        if current is None or item.engagement.score > current.engagement.score:
            best[key] = item
    # Preserve deterministic ordering by (posted_at, author).
    return sorted(best.values(), key=lambda i: (i.posted_at, i.author))


def build_digest(
    symbol: str,
    items: list[SocialItem],
    *,
    baseline_volume: float,
    params: SocialFilterParams,
    now: datetime,
    is_low_liquidity: bool = False,
    score_text: ScoreText | None = None,
) -> SocialDigest:
    """Run Stage-1 filtering + near-dup collapse, then aggregate the survivors
    into a compact digest (bull/bear tally, mean intensity, volume-vs-baseline,
    anomaly flag, top-N sample). Empty/all-junk input yields an empty
    (technical-only) digest."""
    symbol = symbol.upper()
    qualifying = [i for i in items if passes_stage1(i, params)]
    if params.near_dup_enabled:
        qualifying = collapse_near_duplicates(qualifying)

    band = params.sentiment_neutral_band
    bull = sum(1 for i in qualifying if classify_sentiment(i, score_text, band) == "bull")
    bear = sum(1 for i in qualifying if classify_sentiment(i, score_text, band) == "bear")

    # Mean textual intensity, independent of the label-preferring tally above:
    # the tally says which way the crowd leans, this says how hard. Ten mildly
    # bullish posts and two euphoric ones produce the same bull/bear counts.
    # None (not 0.0) when unscored, so "no scorer" never reads as "neutral".
    avg_sentiment: float | None = None
    if score_text is not None and qualifying:
        avg_sentiment = round(sum(score_text(i.text) for i in qualifying) / len(qualifying), 4)
    # Laplace-smoothed so a zero denominator is well-defined and one lone post
    # doesn't produce an infinite ratio.
    bull_bear_ratio = (bull + 1) / (bear + 1)

    mention_volume = len(qualifying)
    baseline = baseline_volume if baseline_volume > 0 else float(mention_volume or 1)
    volume_ratio = mention_volume / baseline

    multiplier = (
        params.low_liquidity_volume_multiplier
        if is_low_liquidity
        else params.anomaly_volume_multiplier
    )
    anomaly_flag = mention_volume >= params.min_posts_for_anomaly and volume_ratio >= multiplier

    # Stamp each sampled post with Stage-1's verdict. Without this the operator
    # sees a blank Sentiment column for every post the source didn't label --
    # i.e. exactly the posts the scorer did the work on.
    top_posts = [
        p.model_copy(update={"classified_sentiment": classify_sentiment(p, score_text, band)})
        for p in sorted(
            qualifying,
            key=lambda i: (i.engagement.score, i.engagement.replies),
            reverse=True,
        )[: params.top_n]
    ]

    return SocialDigest(
        symbol=symbol,
        qualifying_post_count=mention_volume,
        bull_count=bull,
        bear_count=bear,
        bull_bear_ratio=bull_bear_ratio,
        avg_sentiment=avg_sentiment,
        mention_volume=mention_volume,
        baseline_volume=baseline,
        volume_ratio=volume_ratio,
        anomaly_flag=anomaly_flag,
        top_posts=top_posts,
        generated_at=now,
    )
