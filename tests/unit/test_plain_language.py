"""The jargon->plain-English layer: confidence words, tri-state signal text,
headlines, and the one-sentence reason. Pure functions, so unit-tested
directly."""

from __future__ import annotations

from types import SimpleNamespace

from clav.web import plain_language as pl


def test_action_verb_tense() -> None:
    assert pl.action_verb("BUY", executed=True) == "Bought"
    assert pl.action_verb("BUY", executed=False) == "Wants to buy"
    assert pl.action_verb("SELL", executed=True) == "Sold"
    assert pl.action_verb("HOLD", executed=True) == "Held"


def test_confidence_label_bands() -> None:
    assert pl.confidence_label(0.9)["word"] == "High"
    assert pl.confidence_label(0.5)["word"] == "Medium"
    assert pl.confidence_label(0.1)["word"] == "Low"
    assert pl.confidence_label(0.5)["pct"] == 50
    none = pl.confidence_label(None)
    assert none["word"] == "Not rated"
    assert none["pct"] is None


def test_tri_state_dead_band() -> None:
    # within the neutral band -> neutral, not positive/negative
    assert pl.price_momentum(0.05)["tone"] == "neutral"
    assert pl.price_momentum(0.5)["tone"] == "pos"
    assert pl.price_momentum(-0.5)["tone"] == "neg"
    assert pl.news_mood(0.8)["text"] == "Positive news mood"
    assert pl.portfolio_fit(-0.9)["text"] == "Would crowd your holdings"


def test_bar_magnitude_is_0_to_100() -> None:
    assert pl.price_momentum(0.5)["bar"] == 50
    assert pl.price_momentum(2.0)["bar"] == 100  # clamped
    assert pl.confidence_label(0.42)["bar"] == 42


def test_decision_headline() -> None:
    assert (
        pl.decision_headline("AAPL", "BUY", 10, executed=True)
        == "Bought 10 shares of AAPL"
    )
    assert (
        pl.decision_headline("AAPL", "BUY", 1, executed=True) == "Bought 1 share of AAPL"
    )
    assert pl.decision_headline("AAPL", "HOLD", 0, executed=True) == "Held AAPL"


def test_social_mood() -> None:
    assert pl.social_mood(0.8)["text"] == "Bullish social buzz"
    assert pl.social_mood(-0.8)["text"] == "Bearish social buzz"
    assert pl.social_mood(0.0)["tone"] == "neutral"


def test_signal_bars_splits_news_and_social_when_scored() -> None:
    decision = SimpleNamespace(technical_score=0.5, portfolio_bias=0.0)
    llm = {"sentiment": 0.8, "news_sentiment": 0.7, "social_sentiment": -0.6}
    bars = pl.signal_bars(decision, llm)
    assert [b["label"] for b in bars] == [
        "Price trend", "News mood", "Social mood", "Portfolio fit"
    ]
    assert bars[1]["text"] == "Positive news mood"
    assert bars[1]["tone"] == "pos"
    assert bars[2]["text"] == "Bearish social buzz"
    assert bars[2]["tone"] == "neg"


def test_signal_bars_falls_back_to_combined_for_legacy_decisions() -> None:
    # An older decision stored only the blended sentiment (no per-source split).
    decision = SimpleNamespace(technical_score=0.5, portfolio_bias=0.0)
    bars = pl.signal_bars(decision, {"sentiment": 0.8})
    assert [b["label"] for b in bars] == ["Price trend", "News & social mood", "Portfolio fit"]
    assert bars[1]["text"] == "Positive news & social mood"


def test_plain_reason_mixed_vs_leaning() -> None:
    flat = SimpleNamespace(technical_score=0.0, portfolio_bias=0.0)
    assert "mixed" in pl.plain_reason(flat, None)

    leaning = SimpleNamespace(technical_score=0.6, portfolio_bias=0.0)
    reason = pl.plain_reason(leaning, {"sentiment": 0.7, "conviction": 0.8})
    assert "price trending up" in reason
    assert "high" in reason  # confidence word, lowercased
