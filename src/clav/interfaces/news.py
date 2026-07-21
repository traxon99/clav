"""NewsSource — the port for real-world narrative + hard catalysts (Story 3.1).

Two free, keyless adapters live behind this interface (RSS per-symbol feeds and
SEC EDGAR filings) plus an optional, off-by-default NewsAPI adapter — all in
``clav.integrations.news``. The ``NewsItem`` domain model lives in
``clav.domain.models`` (pure Pydantic, no vendor imports) and is re-exported here
for convenience so callers can ``from clav.interfaces.news import NewsItem``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from clav.domain.models import NewsItem

__all__ = ["NewsItem", "NewsSource"]


class NewsSource(ABC):
    @abstractmethod
    def fetch(self, symbol: str, since: datetime) -> list[NewsItem]:
        """News/filing items for ``symbol`` published at or after ``since``.

        Implementations must be **fail-open**: a transient/permanent source
        failure degrades to an empty list (technical-only), never an exception
        that aborts the scan cycle.
        """
