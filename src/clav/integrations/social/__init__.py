"""Free-tier retail-social adapters behind the ``SocialSource`` interface (Story 3.2).

- ``RedditSource`` — public, unauthenticated ``.json`` search endpoints (no app,
  key, or approval; descriptive User-Agent; polite low QPM). OAuth is an optional
  future upgrade, never a prerequisite (epic decision #5 / 2026-07 access terms).
- ``StockTwitsSource`` — public, unauthenticated cashtag symbol stream (keyless).

Both are best-effort and fail-open to an empty list. No vendor SDKs: ``httpx`` +
stdlib JSON only, so nothing leaks into ``domain``/``interfaces``.
"""

from __future__ import annotations

from clav.integrations.social.reddit import RedditSource
from clav.integrations.social.stocktwits import StockTwitsSource

__all__ = ["RedditSource", "StockTwitsSource"]
