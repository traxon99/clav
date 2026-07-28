"""LexiconScorer — VADER plus a finance overlay, behind ``SentimentScorer``.

Why VADER: it is a pure-Python lexicon+rules model (~600 KB, no numpy, no model
download, microseconds per post), so it costs the Pi's 150-350 MB ``clav-core``
budget essentially nothing. It is also the one thing on the classic
"stock-prediction" project shelf that actually transfers here — it handles the
four things a flat word tally cannot:

  negation ("not a squeeze" is not bullish), degree modifiers ("absolutely
  tanking" > "tanking"), emphasis (ALL CAPS, "!!!", emoji), and the contrastive
  "but" ("decent earnings but guidance is trash" resolves bearish).

Why the overlay: VADER is trained on general social text and is actively wrong
on finance vocabulary. "beat" reads as violence-negative when an earnings beat
is strongly bullish; "rip" reads as R.I.P. when "ripping" means going up; "miss"
is a mild sadness term rather than an earnings miss; and the retail options /
WSB vocabulary ("puts", "tendies", "bagholder", "rug", "squeeze") is absent
entirely. ``_FINANCE_LEXICON`` overrides those in VADER's own [-4, +4] scale.

Polarity signs for the non-slang terms follow the Loughran-McDonald finance
sentiment categories (the accounting-literature standard for exactly this
general-vs-finance mismatch); only the ~80 terms that actually show up in retail
chatter are transcribed, rather than vendoring their 80k-word dictionary.
"""

from __future__ import annotations

from clav.common.logging import get_logger
from clav.interfaces.sentiment import SentimentScorer

_logger = get_logger(__name__)

# VADER's lexicon scale is [-4, +4]. Entries here are either corrections of a
# wrong VADER valence or additions of vocabulary it has never seen.
_FINANCE_LEXICON: dict[str, float] = {
    # --- Earnings / analyst actions (LM: Positive / Negative) -----------------
    # VADER scores "beat"/"beats" negative (to strike). An earnings beat is the
    # single most bullish word in this corpus.
    "beat": 2.5,
    "beats": 2.5,
    "outperform": 2.5,
    "upgrade": 2.5,
    "upgraded": 2.5,
    "raised": 1.5,
    "guidance": 0.0,  # direction comes from its modifiers, not the word
    "miss": -2.5,  # VADER reads this as "I miss you"
    "missed": -2.5,
    "misses": -2.5,
    "downgrade": -2.5,
    "downgraded": -2.5,
    "underperform": -2.5,
    "cut": -1.5,
    "slashed": -2.0,
    # --- Direction / price action -------------------------------------------
    "rip": 2.0,  # VADER reads this as R.I.P.
    "rips": 2.0,
    "ripping": 2.5,
    "ripped": 2.0,
    "breakout": 2.5,
    "rally": 2.0,
    "rallying": 2.5,
    "surge": 2.5,
    "surging": 2.5,
    "soar": 2.5,
    "soaring": 2.5,
    "green": 1.5,
    "undervalued": 2.0,
    "oversold": 1.5,
    "dump": -2.0,
    "dumping": -2.5,
    "tank": -2.0,  # VADER: neutral (the vehicle)
    "tanking": -2.5,
    "tanked": -2.5,
    "plunge": -2.5,
    "plunging": -2.5,
    "collapse": -2.5,
    "collapsing": -3.0,
    "overvalued": -2.0,
    "overbought": -1.5,
    "red": -1.5,
    "bleeding": -2.0,
    # --- Positions / options -------------------------------------------------
    "bull": 2.0,
    "bullish": 2.5,
    "calls": 1.5,
    "long": 1.0,
    "longs": 1.0,
    "buy": 1.5,
    "buying": 1.5,
    "bought": 1.0,
    "accumulate": 1.5,
    "accumulating": 1.5,
    "bear": -2.0,
    "bearish": -2.5,
    "puts": -1.5,
    "short": -1.5,  # VADER: neutral (length)
    "shorts": -1.5,
    "shorting": -2.0,
    "sell": -1.5,
    "selling": -1.5,
    "sold": -1.0,
    "avoid": -1.5,
    # NB: bare "call" and "put" are omitted deliberately -- both are far more
    # often ordinary verbs than option contracts, and mis-signing them would
    # poison a large share of posts.
    # --- Retail slang (absent from VADER entirely) ---------------------------
    "moon": 2.5,
    "mooning": 3.0,
    "moonshot": 2.5,
    "tendies": 2.5,
    "hodl": 1.5,
    "squeeze": 2.0,
    "squeezing": 2.5,
    "bagholder": -2.5,
    "bagholders": -2.5,
    "bagholding": -2.5,
    "rug": -3.0,
    "rugged": -3.0,
    "rugpull": -3.5,
    "dumpster": -2.5,
    # --- Corporate-event risk (LM: Negative / Litigious) ---------------------
    "dilution": -2.0,
    "diluting": -2.0,
    "offering": -1.5,  # a share offering, i.e. dilution
    "halted": -2.5,
    "halt": -2.0,
    "bankruptcy": -3.5,
    "bankrupt": -3.5,
    "insolvent": -3.5,
    "delisted": -3.0,
    "delisting": -3.0,
    "restatement": -2.5,
    "investigation": -2.0,
    "subpoena": -2.5,
    "lawsuit": -2.0,
    "litigation": -2.0,
    "probe": -2.0,
    "recall": -2.0,
}


class LexiconScorer(SentimentScorer):
    """VADER compound score in ``[-1, +1]``, with the finance overlay applied.

    Construction loads VADER's ~7.5k-entry lexicon once (a few MB transient,
    well under a megabyte resident) and is safe to share across threads — the
    analyzer holds no per-call state.
    """

    def __init__(self, *, overlay: dict[str, float] | None = None) -> None:
        # Imported lazily so the package is only a hard requirement when the
        # lexicon scorer is actually selected (config: sources.social.scorer).
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

        self._analyzer = SentimentIntensityAnalyzer()
        self._analyzer.lexicon.update(_FINANCE_LEXICON if overlay is None else overlay)

    def score(self, text: str) -> float:
        if not text or not text.strip():
            return 0.0
        try:
            compound = self._analyzer.polarity_scores(text)["compound"]
        except Exception as exc:  # fail-open: never abort a scan cycle
            _logger.warning("sentiment_score_failed", error=str(exc))
            return 0.0
        return max(-1.0, min(1.0, float(compound)))
