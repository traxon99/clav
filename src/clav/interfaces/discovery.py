"""DiscoverySource — the port for cheap, keyless "what's buzzing right now"
candidate feeds (autonomous-discovery epic).

The funnel: a discovery source cheaply surfaces a handful of trending tickers
across the whole market (one HTTP call, no per-symbol fan-out), the
``DiscoveryService`` validates + ranks + caps them, and only that small shortlist
reaches the expensive news+social+Gemini analyst. This is what keeps "trade on
market sentiment across all of Alpaca" within the Gemini budget and the Pi's RAM.

Adapters (StockTwits trending, Reddit movers — both public/keyless) return
normalized ``DiscoveryCandidate``s. Fail-open, exactly like ``SocialSource``: a
dead/rate-limited/blocked source returns an empty list, never an exception that
aborts a scan cycle.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from clav.domain.models import DiscoveryCandidate

__all__ = ["DiscoveryCandidate", "DiscoverySource"]


class DiscoverySource(ABC):
    @abstractmethod
    def fetch(self) -> list[DiscoveryCandidate]:
        """Trending/high-buzz candidate tickers right now (market-wide, not
        per-symbol). Fail-open: return an empty list on any failure."""
