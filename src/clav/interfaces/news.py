"""NewsSource — stubbed for Epic 1. Real adapters (NewsAPI/RSS/EDGAR) and the
NewsItem domain model land in Epic 3 (docs/epics — see docs/02-modules.md §2).
Epic 1's ScanCycleService never wires a NewsSource in; DecisionEngine is called
with llm_signal hardcoded to 0.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel


class NewsItem(BaseModel):
    """Placeholder shape; fleshed out in Epic 3."""

    symbol: str
    source: str
    headline: str
    published_at: datetime
    content_hash: str


class NewsSource(ABC):
    @abstractmethod
    def fetch(self, symbol: str, since: datetime) -> list[NewsItem]:
        """News items for ``symbol`` published after ``since``."""
