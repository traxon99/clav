"""StockTwitsSource — public, unauthenticated cashtag symbol stream (Story 3.2).

Reads ``streams/symbol/{symbol}.json`` (keyless). StockTwits supplies an explicit
per-message Bullish/Bearish label and a real follower count, so both the Stage-1
reputation floor and the bull/bear tally have genuine inputs here. New API
registrations are paused as of 2026-07 and the public endpoint is unstable — so
this is strictly best-effort and fail-open.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from clav.clock import Clock
from clav.common.logging import get_logger
from clav.domain.models import Engagement, SocialItem, SocialSentiment
from clav.integrations.news.http import HttpTextFetcher, TextFetcher
from clav.interfaces.social import SocialSource

_logger = get_logger(__name__)

_SENTIMENT_MAP: dict[str, SocialSentiment] = {"Bullish": "bull", "Bearish": "bear"}


def _parse_created_at(raw: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime(1970, 1, 1, tzinfo=UTC)
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


class StockTwitsSource(SocialSource):
    def __init__(
        self,
        *,
        clock: Clock,
        url_template: str = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json",
        fetcher: TextFetcher | None = None,
    ) -> None:
        self._clock = clock
        self._url_template = url_template
        self._fetcher = fetcher or HttpTextFetcher()

    def fetch(self, symbol: str, since: datetime) -> list[SocialItem]:
        symbol = symbol.upper()
        try:
            body = self._fetcher.get(self._url_template.format(symbol=symbol))
            payload = json.loads(body)
            messages = payload["messages"]
        except Exception as exc:  # fail-open: best-effort source
            _logger.warning("stocktwits_fetch_failed", symbol=symbol, error=str(exc))
            return []

        items: list[SocialItem] = []
        for msg in messages:
            text = (msg.get("body") or "").strip()
            if not text:
                continue
            posted_at = _parse_created_at(msg.get("created_at", ""))
            if posted_at < since:
                continue
            user = msg.get("user") or {}
            entities = msg.get("entities") or {}
            basic = ((entities.get("sentiment") or {}).get("basic")) if entities else None
            likes = (msg.get("likes") or {}).get("total", 0)
            items.append(
                SocialItem(
                    symbol=symbol,
                    text=text,
                    author=user.get("username") or "unknown",
                    author_reputation=float(user.get("followers", 0) or 0),
                    engagement=Engagement(
                        score=int(likes or 0),
                        replies=int(msg.get("conversation", {}).get("replies", 0) or 0)
                        if isinstance(msg.get("conversation"), dict)
                        else 0,
                    ),
                    posted_at=posted_at,
                    source="stocktwits",
                    sentiment=_SENTIMENT_MAP.get(basic) if basic else None,
                )
            )
        return items
