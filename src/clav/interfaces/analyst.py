"""Analyst — stubbed for Epic 1. GeminiAnalyst and the full AnalysisResult/
TradeReview schemas land in Epic 3 & 5 (docs/02-modules.md §3, docs/07-trade-review.md).
Epic 1's ScanCycleService never wires an Analyst in; DecisionEngine is called with
llm_signal hardcoded to 0.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from clav.interfaces.news import NewsItem


class AnalysisResult(BaseModel):
    """Placeholder shape; fleshed out in Epic 3."""

    symbol: str
    confidence: float
    llm_signal: float


class TradeReview(BaseModel):
    """Placeholder shape; fleshed out in Epic 5."""

    trade_id: str
    summary: str


class Analyst(ABC):
    @abstractmethod
    def analyze(self, symbol: str, news: list[NewsItem], context: dict[str, Any]) -> AnalysisResult:
        """Turn a news bundle into a bounded, structured AnalysisResult."""

    @abstractmethod
    def review(self, trade_id: str, context: dict[str, Any]) -> TradeReview:
        """Post-trade journal entry for a closed trade."""
