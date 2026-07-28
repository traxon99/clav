"""Graded text-sentiment adapters behind the ``SentimentScorer`` interface.

- ``LexiconScorer`` — VADER + a finance overlay. Pure Python, no numpy, no model
  download; the Pi-safe default. See ``lexicon.py`` for why VADER alone is not
  enough on finance text.
"""

from __future__ import annotations

from clav.integrations.sentiment.lexicon import LexiconScorer

__all__ = ["LexiconScorer"]
