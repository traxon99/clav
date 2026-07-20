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
from dataclasses import dataclass
from datetime import datetime

from clav.domain.models import SocialDigest, SocialItem, SocialSentiment

_CASHTAG_RE = re.compile(r"\$[A-Za-z]{1,6}\b")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# Small deterministic sentiment lexicon for posts lacking an explicit label
# (Reddit). This is intentionally crude — Gemini's Stage-2 judgement (Story 3.4)
# is where nuance lives; Stage 1 only needs a cheap bull/bear tally.
_BULL_WORDS = frozenset(
    {
        "buy", "buying", "bought", "bull", "bullish", "long", "calls", "call",
        "moon", "rocket", "squeeze", "breakout", "rally", "undervalued", "up",
        "green", "rip", "surge", "beat", "beats", "upgrade", "accumulate",
    }
)
_BEAR_WORDS = frozenset(
    {
        "sell", "selling", "sold", "bear", "bearish", "short", "puts", "put",
        "dump", "crash", "drop", "tank", "collapse", "overvalued", "down",
        "red", "miss", "misses", "downgrade", "avoid", "bagholder",
    }
)
_WORD_RE = re.compile(r"[a-z']+")


@dataclass(frozen=True)
class SocialFilterParams:
    min_engagement_score: int = 5
    min_replies: int = 0
    min_author_reputation: float = 50.0
    max_symbols_per_post: int = 5
    promo_keywords: tuple[str, ...] = (
        "pump", "guaranteed", "to the moon", "join my", "free alert",
        "dm me", "sign up", "not financial advice but", "100x", "1000%",
    )
    promo_link_domains: tuple[str, ...] = (
        "discord.gg", "discord.com", "t.me", "telegram", "bit.ly",
        "patreon", "substack", "onlyfans", "linktr.ee",
    )
    near_dup_enabled: bool = True
    top_n: int = 5
    anomaly_volume_multiplier: float = 3.0
    low_liquidity_volume_multiplier: float = 2.0
    min_posts_for_anomaly: int = 5


def classify_sentiment(item: SocialItem) -> SocialSentiment:
    """Explicit label wins; otherwise a cheap bull/bear word tally (ties -> neutral)."""
    if item.sentiment is not None:
        return item.sentiment
    words = _WORD_RE.findall(item.text.lower())
    bull = sum(1 for w in words if w in _BULL_WORDS)
    bear = sum(1 for w in words if w in _BEAR_WORDS)
    if bull > bear:
        return "bull"
    if bear > bull:
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
) -> SocialDigest:
    """Run Stage-1 filtering + near-dup collapse, then aggregate the survivors
    into a compact digest (bull/bear tally, volume-vs-baseline, anomaly flag,
    top-N sample). Empty/all-junk input yields an empty (technical-only) digest."""
    symbol = symbol.upper()
    qualifying = [i for i in items if passes_stage1(i, params)]
    if params.near_dup_enabled:
        qualifying = collapse_near_duplicates(qualifying)

    bull = sum(1 for i in qualifying if classify_sentiment(i) == "bull")
    bear = sum(1 for i in qualifying if classify_sentiment(i) == "bear")
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
    anomaly_flag = (
        mention_volume >= params.min_posts_for_anomaly and volume_ratio >= multiplier
    )

    top_posts = sorted(
        qualifying,
        key=lambda i: (i.engagement.score, i.engagement.replies),
        reverse=True,
    )[: params.top_n]

    return SocialDigest(
        symbol=symbol,
        qualifying_post_count=mention_volume,
        bull_count=bull,
        bear_count=bear,
        bull_bear_ratio=bull_bear_ratio,
        mention_volume=mention_volume,
        baseline_volume=baseline,
        volume_ratio=volume_ratio,
        anomaly_flag=anomaly_flag,
        top_posts=top_posts,
        generated_at=now,
    )
