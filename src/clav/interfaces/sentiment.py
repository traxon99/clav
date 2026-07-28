"""SentimentScorer — the port for graded text sentiment used by Stage-1.

``clav.domain.social`` classifies every post that lacks an explicit source label
(i.e. every Reddit post). Its built-in fallback is a flat bull-word/bear-word
tally, which cannot see negation, intensity, or emphasis. This port lets the
composition root inject a real scorer without dragging a third-party package
into ``domain`` — same arrangement as ``SocialSource``/``NewsSource``.

Implementations live in ``clav.integrations.sentiment``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

__all__ = ["SentimentScorer"]


class SentimentScorer(ABC):
    @abstractmethod
    def score(self, text: str) -> float:
        """Graded sentiment in ``[-1.0, +1.0]`` (negative bearish, positive bullish).

        Fail-open: an unscoreable input returns ``0.0`` (neutral), never an
        exception — Stage-1 runs inside the scan cycle and must not abort it.
        """
