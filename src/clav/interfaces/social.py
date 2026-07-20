"""SocialSource — the port for free-tier retail social "vibes" (Story 3.2).

Adapters (Reddit, StockTwits — both public/keyless) return normalized
``SocialItem``s. The compact, manipulation-resistant ``SocialDigest`` that
actually reaches Gemini is built from the combined items by the pure Stage-1
aggregator in ``clav.domain.social`` — sources emit items, the domain aggregates.

``SocialItem`` / ``SocialDigest`` are pure domain models in
``clav.domain.models`` (re-exported here for convenience).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from clav.domain.models import SocialDigest, SocialItem

__all__ = ["SocialDigest", "SocialItem", "SocialSource"]


class SocialSource(ABC):
    @abstractmethod
    def fetch(self, symbol: str, since: datetime) -> list[SocialItem]:
        """Normalized posts mentioning ``symbol`` at/after ``since``.

        Fail-open: a dead/rate-limited/blocked source returns an empty list
        (which aggregates to an empty, technical-only digest), never an
        exception that aborts the scan cycle.
        """
