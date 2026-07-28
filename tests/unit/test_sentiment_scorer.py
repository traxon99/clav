"""Graded Stage-1 sentiment: the VADER + finance-overlay scorer, and its effect
on ``domain.social`` aggregation.

Every case here is one the word-tally fallback gets *wrong* — that's the point of
the scorer. See ``clav.integrations.sentiment.lexicon`` for why the overlay exists.
"""

from __future__ import annotations

from datetime import UTC, datetime

from clav.domain.models import Engagement, SocialItem
from clav.domain.social import (
    SocialFilterParams,
    band_sentiment,
    build_digest,
    classify_sentiment,
)
from clav.integrations.sentiment import LexiconScorer

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
PARAMS = SocialFilterParams()
SCORE = LexiconScorer().score


def _item(
    text: str,
    *,
    score: int = 100,
    author: str = "alice",
    sentiment: str | None = None,
) -> SocialItem:
    return SocialItem(
        symbol="AAPL",
        text=text,
        author=author,
        author_reputation=500.0,
        engagement=Engagement(score=score, replies=10),
        posted_at=NOW,
        source="reddit:test",
        sentiment=sentiment,  # type: ignore[arg-type]
    )


# --- scorer contract ---------------------------------------------------------


def test_score_is_bounded_and_empty_text_is_neutral() -> None:
    assert SCORE("") == 0.0
    assert SCORE("   ") == 0.0
    for text in ["ABSOLUTELY MOONING!!! 🚀🚀🚀", "bankrupt fraud rugpull delisted disaster"]:
        assert -1.0 <= SCORE(text) <= 1.0


def test_scorer_fails_open_instead_of_aborting_the_cycle() -> None:
    """Stage-1 runs inside the scan cycle: a broken analyzer must degrade this
    symbol to neutral, never raise out and kill the cycle."""

    class Exploding:
        def polarity_scores(self, text: str) -> dict[str, float]:
            raise RuntimeError("lexicon corrupted")

    scorer = LexiconScorer()
    scorer._analyzer = Exploding()  # type: ignore[assignment]
    assert scorer.score("they beat earnings") == 0.0

    # ...and the digest built from it is still well-formed.
    digest = build_digest(
        "AAPL",
        [_item("they beat earnings")],
        baseline_volume=1.0,
        params=PARAMS,
        now=NOW,
        score_text=scorer.score,
    )
    assert digest.qualifying_post_count == 1
    assert digest.avg_sentiment == 0.0


def test_negation_flips_direction() -> None:
    """The flat word tally sees 'squeeze' and calls this bullish."""
    assert SCORE("this is not a squeeze") <= 0.0
    assert SCORE("this is a squeeze") > 0.0


def test_intensity_is_graded_not_binary() -> None:
    """The tally scores both of these identically; magnitude is the new signal."""
    assert SCORE("MOONING 🚀🚀🚀") > SCORE("up a bit today")
    assert SCORE("absolutely tanking") < SCORE("drifting down")


def test_contrastive_but_resolves_to_the_second_clause() -> None:
    assert SCORE("earnings were decent but guidance is a disaster") < 0.0


# --- finance overlay: cases where stock VADER is actively wrong ---------------


def test_earnings_beat_reads_bullish_not_violent() -> None:
    """Stock VADER scores 'beat' negative (to strike someone)."""
    assert SCORE("they beat earnings") > 0.0
    assert SCORE("they missed earnings") < 0.0


def test_rip_is_price_action_not_a_eulogy() -> None:
    """Stock VADER reads 'rip' as R.I.P."""
    assert SCORE("this thing is ripping today") > 0.0


def test_options_and_position_vocabulary_is_directional() -> None:
    """Stock VADER is neutral on all of these."""
    assert SCORE("loading up on puts") < 0.0
    assert SCORE("shorting this") < 0.0


def test_retail_slang_absent_from_vader_is_scored() -> None:
    assert SCORE("tendies incoming") > 0.0
    assert SCORE("total rugpull, I'm a bagholder now") < 0.0


def test_corporate_event_risk_is_bearish() -> None:
    assert SCORE("dilution from the new offering") < 0.0
    assert SCORE("trading halted pending investigation") < 0.0


# --- banding + aggregation ---------------------------------------------------


def test_band_sentiment_dead_band() -> None:
    assert band_sentiment(0.5) == "bull"
    assert band_sentiment(-0.5) == "bear"
    assert band_sentiment(0.0) == "neutral"
    assert band_sentiment(0.04) == "neutral"  # inside the default 0.05 band
    assert band_sentiment(0.04, neutral_band=0.01) == "bull"


def test_classify_uses_scorer_but_explicit_label_still_wins() -> None:
    assert classify_sentiment(_item("not a squeeze, avoid"), SCORE) == "bear"
    # StockTwits' own Bullish/Bearish label outranks the text every time.
    assert classify_sentiment(_item("collapsing dumpster fire", sentiment="bull"), SCORE) == "bull"


def test_scorer_fixes_a_tally_misread_in_the_digest() -> None:
    """Same posts, both paths: the tally counts bull words, the scorer reads them."""
    items = [
        _item("this is not a breakout", author="a"),
        _item("no rally here", author="b"),
    ]
    tally = build_digest("AAPL", items, baseline_volume=2.0, params=PARAMS, now=NOW)
    graded = build_digest(
        "AAPL", items, baseline_volume=2.0, params=PARAMS, now=NOW, score_text=SCORE
    )
    assert tally.bull_count == 2 and tally.bear_count == 0  # wrong, negation-blind
    assert graded.bear_count == 2 and graded.bull_count == 0


def test_avg_sentiment_separates_mild_agreement_from_euphoria() -> None:
    mild = [_item("looks ok, might buy a little", author=f"u{i}") for i in range(4)]
    manic = [_item("ABSOLUTELY MOONING 🚀🚀 TENDIES!!!", author=f"u{i}") for i in range(4)]
    mild_digest = build_digest(
        "AAPL", mild, baseline_volume=4.0, params=PARAMS, now=NOW, score_text=SCORE
    )
    manic_digest = build_digest(
        "AAPL", manic, baseline_volume=4.0, params=PARAMS, now=NOW, score_text=SCORE
    )
    # Identical direction tallies...
    assert mild_digest.bull_count == manic_digest.bull_count
    # ...but the intensity Gemini now sees is not the same.
    assert mild_digest.avg_sentiment is not None
    assert manic_digest.avg_sentiment is not None
    assert manic_digest.avg_sentiment > mild_digest.avg_sentiment


def test_avg_sentiment_is_none_when_unscored() -> None:
    """None, not 0.0 -- 'not measured' must not read to Gemini as 'neutral'."""
    items = [_item("buy bullish calls")]
    unscored = build_digest("AAPL", items, baseline_volume=1.0, params=PARAMS, now=NOW)
    assert unscored.avg_sentiment is None
    empty = build_digest("AAPL", [], baseline_volume=1.0, params=PARAMS, now=NOW, score_text=SCORE)
    assert empty.avg_sentiment is None


def test_top_posts_carry_stage1s_verdict_with_provenance() -> None:
    """The operator's Sentiment column was blank for every unlabelled post --
    i.e. exactly the ones the scorer judged. Stamp the verdict, but keep it
    distinct from what the source itself claimed."""
    items = [
        _item("not selling a single share", author="a", score=90),
        _item("collapsing, total disaster", author="b", score=80, sentiment="bull"),
    ]
    digest = build_digest(
        "AAPL", items, baseline_volume=2.0, params=PARAMS, now=NOW, score_text=SCORE
    )
    scored, labelled = digest.top_posts[0], digest.top_posts[1]

    # Unlabelled post: source said nothing, Stage 1 concluded bull.
    assert scored.sentiment is None
    assert scored.classified_sentiment == "bull"

    # Labelled post: the source's own label wins and both agree, so the UI can
    # tell "the source said this" from "we worked it out".
    assert labelled.sentiment == "bull"
    assert labelled.classified_sentiment == "bull"


def test_unscored_digest_still_stamps_top_posts() -> None:
    """The word-tally fallback must populate the column too, or turning the
    scorer off silently blanks the dashboard."""
    digest = build_digest(
        "AAPL", [_item("buy bullish calls")], baseline_volume=1.0, params=PARAMS, now=NOW
    )
    assert digest.top_posts[0].classified_sentiment == "bull"
