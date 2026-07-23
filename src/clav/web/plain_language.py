"""Plain-language translation layer for the forward-facing dashboard.

The trading engine speaks in signed scores (``raw_score``, ``technical_score``,
``llm_signal``, ``portfolio_bias``, ``conviction``, ``drawdown`` …). Those are
the right vocabulary for an operator tuning the machine, but they are noise to
someone who just wants to know *what the bot did and why*. This module is the
single place that maps those numbers to words a non-specialist can read —
"Positive news mood", "Confidence: High", "Price trending up" — plus a small
0-100 bar magnitude so the same signal can be drawn as a bar instead of a raw
float.

Nothing here touches the database or changes a decision; it is a pure,
side-effect-free presentation helper so the operator-facing pages (Epic 3/4)
keep the exact scores while the forward-facing pages read in plain English.
"""

from __future__ import annotations

from typing import Any

# Scores in this system are broadly in [-1, 1]. A small dead-band keeps a
# near-zero signal from being dressed up as "positive"/"negative" when it is
# really just noise.
_NEUTRAL_BAND = 0.15


def action_verb(action: str, *, executed: bool = True) -> str:
    """"BUY" -> "Bought"/"Wants to buy", etc. — past tense once it actually
    happened, intent otherwise."""
    a = (action or "").upper()
    if a == "BUY":
        return "Bought" if executed else "Wants to buy"
    if a == "SELL":
        return "Sold" if executed else "Wants to sell"
    return "Held" if executed else "Holding"


def action_tone(action: str) -> str:
    """A CSS tone class (``pos``/``neg``/``neutral``) for coloring an action."""
    a = (action or "").upper()
    if a == "BUY":
        return "pos"
    if a == "SELL":
        return "neg"
    return "neutral"


def confidence_label(conviction: float | None) -> dict[str, Any]:
    """The LLM's ``conviction`` (0-1) as a High/Medium/Low word plus a percent
    and a 0-100 bar magnitude. ``None`` (technical-only decision) reads as
    "Not rated"."""
    if conviction is None:
        return {"word": "Not rated", "tone": "neutral", "pct": None, "bar": 0}
    pct = round(max(0.0, min(1.0, conviction)) * 100)
    if conviction >= 0.66:
        word, tone = "High", "pos"
    elif conviction >= 0.33:
        word, tone = "Medium", "neutral"
    else:
        word, tone = "Low", "neg"
    return {"word": word, "tone": tone, "pct": pct, "bar": pct}


def _tri_state(value: float | None, positive: str, negative: str, neutral: str) -> dict[str, Any]:
    """A signed score -> {text, tone, bar} where ``bar`` is 0-100 magnitude."""
    if value is None:
        return {"text": neutral, "tone": "neutral", "bar": 0}
    bar = round(min(1.0, abs(value)) * 100)
    if value > _NEUTRAL_BAND:
        return {"text": positive, "tone": "pos", "bar": bar}
    if value < -_NEUTRAL_BAND:
        return {"text": negative, "tone": "neg", "bar": bar}
    return {"text": neutral, "tone": "neutral", "bar": bar}


def news_mood(sentiment: float | None) -> dict[str, Any]:
    """``news_sentiment`` -> "Positive/Negative/Neutral news mood" (the model's
    read of the news/filings block on its own)."""
    return _tri_state(
        sentiment,
        "Positive news mood",
        "Negative news mood",
        "Neutral news mood",
    )


def social_mood(social_sentiment: float | None) -> dict[str, Any]:
    """``social_sentiment`` -> "Bullish/Bearish/Mixed social buzz" (the model's
    read of the Reddit/StockTwits digest on its own)."""
    return _tri_state(
        social_sentiment,
        "Bullish social buzz",
        "Bearish social buzz",
        "Mixed social buzz",
    )


def combined_mood(sentiment: float | None) -> dict[str, Any]:
    """The single blended read, for older decisions that predate the news/social
    split (only the combined ``sentiment`` was stored)."""
    return _tri_state(
        sentiment,
        "Positive news & social mood",
        "Negative news & social mood",
        "Neutral news & social mood",
    )


def price_momentum(technical_score: float | None) -> dict[str, Any]:
    """``technical_score`` -> "Price trending up/down / holding steady"."""
    return _tri_state(
        technical_score,
        "Price trending up",
        "Price trending down",
        "Price holding steady",
    )


def portfolio_fit(portfolio_bias: float | None) -> dict[str, Any]:
    """``portfolio_bias`` -> whether the trade fits or crowds the current mix."""
    return _tri_state(
        portfolio_bias,
        "Fits your current mix",
        "Would crowd your holdings",
        "Neutral for your mix",
    )


def signal_bars(decision: Any, llm: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The human-readable drivers behind a decision, each as a labelled bar
    (label, plain text, tone, 0-100 magnitude) — the "what the bot is reading"
    panel. Price trend leads and Portfolio fit trails; in between sit *separate*
    News mood and Social mood bars when the model scored them individually
    (``news_sentiment``/``social_sentiment``), falling back to a single combined
    bar for older decisions that only stored the blended ``sentiment``."""
    bars: list[dict[str, Any]] = [
        {"label": "Price trend", **price_momentum(getattr(decision, "technical_score", None))},
    ]
    news_s = llm.get("news_sentiment") if llm else None
    social_s = llm.get("social_sentiment") if llm else None
    if news_s is not None or social_s is not None:
        bars.append({"label": "News mood", **news_mood(news_s)})
        bars.append({"label": "Social mood", **social_mood(social_s)})
    else:
        sentiment = llm.get("sentiment") if llm else None
        bars.append({"label": "News & social mood", **combined_mood(sentiment)})
    fit = portfolio_fit(getattr(decision, "portfolio_bias", None))
    bars.append({"label": "Portfolio fit", **fit})
    return bars


def decision_headline(symbol: str, action: str, qty: int | None, *, executed: bool) -> str:
    """"Bought 10 shares of AAPL" — the one-line, jargon-free summary."""
    verb = action_verb(action, executed=executed)
    if (action or "").upper() == "HOLD" or not qty:
        return f"{verb} {symbol}"
    shares = "share" if abs(qty) == 1 else "shares"
    return f"{verb} {abs(qty)} {shares} of {symbol}"


def plain_reason(decision: Any, llm: dict[str, Any] | None) -> str:
    """A single sentence a non-specialist can read: which drivers pushed the
    bot toward its call. Falls back gracefully when there was no AI signal."""
    bars = signal_bars(decision, llm)
    leaning = [b for b in bars if b["tone"] != "neutral"]
    if not leaning:
        return "The signals were mixed, so the bot stayed cautious."
    phrases = [b["text"].lower() for b in leaning]
    head = ", ".join(phrases[:-1])
    drivers = f"{head} and {phrases[-1]}" if head else phrases[-1]
    conf = confidence_label(llm.get("conviction") if llm else None)
    if conf["pct"] is not None:
        return f"Mostly because {drivers} — the AI's confidence was {conf['word'].lower()}."
    return f"Mostly because {drivers}."
