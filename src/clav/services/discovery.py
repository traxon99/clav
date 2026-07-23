"""DiscoveryService — the funnel that turns market-wide buzz into a small,
budget-bounded shortlist of tickers for the analyst (autonomous-discovery epic).

Each cycle: pull candidates from the cheap keyless sources (fail-open), merge by
symbol, drop anything below the buzz floor / already pinned / already held /
not tradable, rank by buzz, and take the top ``max_candidates_per_cycle``. Only
that shortlist reaches the expensive news+social+Gemini path in
``ScanCycleService`` — which is what keeps "trade on sentiment across all of
Alpaca" inside the token budget and the Pi's RAM.

Pure orchestration over the sources + repos; it persists a compact
``discovery_snapshot`` (symbol + buzz + source) into ``system_control`` so the
``clav-web`` "the bot found these" board can render what was surfaced and why.
"""

from __future__ import annotations

import json
from datetime import datetime

from clav.clock import Clock
from clav.common.logging import get_logger
from clav.data.repositories import Repositories
from clav.domain.models import DiscoveryCandidate
from clav.interfaces.discovery import DiscoverySource

_logger = get_logger(__name__)

DISCOVERY_SNAPSHOT_KEY = "discovery_snapshot"


class DiscoveryService:
    def __init__(
        self,
        sources: list[DiscoverySource],
        *,
        clock: Clock,
        max_candidates_per_cycle: int = 8,
        min_score: float = 0.0,
        exclude_open_positions: bool = True,
    ) -> None:
        self._sources = sources
        self._clock = clock
        self._max_candidates = max_candidates_per_cycle
        self._min_score = min_score
        self._exclude_open_positions = exclude_open_positions

    def _gather(self) -> dict[str, DiscoveryCandidate]:
        """One entry per symbol, keeping the strongest buzz seen across sources."""
        merged: dict[str, DiscoveryCandidate] = {}
        for source in self._sources:
            try:
                candidates = source.fetch()
            except Exception as exc:  # defensive: sources are already fail-open
                _logger.warning("discovery_source_failed", error=str(exc))
                continue
            for c in candidates:
                symbol = c.symbol.strip().upper()
                if not symbol:
                    continue
                current = merged.get(symbol)
                if current is None or c.score > current.score:
                    merged[symbol] = c.model_copy(update={"symbol": symbol})
        return merged

    def candidates_for_cycle(
        self,
        repos: Repositories,
        *,
        pins: set[str],
        open_symbols: set[str],
    ) -> list[str]:
        """The ranked, capped, deduped shortlist of NEW symbols to analyze this
        cycle. Records a snapshot for the UI as a side effect. Never raises."""
        merged = self._gather()
        pins_u = {s.upper() for s in pins}
        open_u = {s.upper() for s in open_symbols}
        have_catalog = repos.assets.count() > 0

        eligible: list[DiscoveryCandidate] = []
        for c in merged.values():
            if c.score < self._min_score:
                continue
            if c.symbol in pins_u:
                continue
            if self._exclude_open_positions and c.symbol in open_u:
                continue
            # Only trust the catalog to reject a symbol once it's actually been
            # populated -- an unrefreshed (empty) catalog must not veto discovery.
            if have_catalog and not repos.assets.is_tradable(c.symbol):
                continue
            eligible.append(c)

        eligible.sort(key=lambda c: c.score, reverse=True)
        shortlist = eligible[: self._max_candidates]

        self._persist_snapshot(repos, shortlist, now=self._clock.now())
        if shortlist:
            _logger.info(
                "discovery_shortlist",
                count=len(shortlist),
                symbols=[c.symbol for c in shortlist],
            )
        return [c.symbol for c in shortlist]

    def _persist_snapshot(
        self, repos: Repositories, shortlist: list[DiscoveryCandidate], *, now: datetime
    ) -> None:
        snapshot = {
            "generated_at": now.isoformat(),
            "candidates": [
                {
                    "symbol": c.symbol,
                    "score": c.score,
                    "mention_volume": c.mention_volume,
                    "anomaly_flag": c.anomaly_flag,
                    "source": c.source,
                }
                for c in shortlist
            ],
        }
        repos.system_control.set(
            DISCOVERY_SNAPSHOT_KEY,
            json.dumps(snapshot),
            updated_at=now,
            updated_by="discovery",
        )
