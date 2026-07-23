"""The forward-facing watchlist view: the symbols the bot is currently
watching, each with a last price, a signed daily change, and a small
hoverable price line — Robinhood's "lists" pane, built from the candles
already persisted per cycle.

The *effective* watchlist is the operator override when one is set, otherwise
the boot-time ``config.yaml`` list (same merge order ``ScanCycleService`` uses).
Adding/removing a symbol edits only the override's ``watchlist`` field and
leaves any weights/risk overrides untouched (see ``ui.py``). Current price is
the last known daily close (clav-web never calls the broker), consistent with
``positions_view``.
"""

from __future__ import annotations

from typing import Any

from clav.data.repositories import Repositories
from clav.web.charts import interactive_line_chart

# How many daily closes back the mini price line reaches. Bounded like every
# other dashboard query (Pi RAM discipline).
_SPARK_DAYS = 30

# A curated set of widely-held tickers so the "add to watchlist" search can
# autocomplete without any network call or vendored dataset. Symbols already
# known to the instrument table are merged in on top of this at request time.
COMMON_TICKERS: list[dict[str, str]] = [
    {"symbol": "AAPL", "name": "Apple"},
    {"symbol": "MSFT", "name": "Microsoft"},
    {"symbol": "GOOGL", "name": "Alphabet (Google)"},
    {"symbol": "AMZN", "name": "Amazon"},
    {"symbol": "NVDA", "name": "NVIDIA"},
    {"symbol": "META", "name": "Meta (Facebook)"},
    {"symbol": "TSLA", "name": "Tesla"},
    {"symbol": "BRK.B", "name": "Berkshire Hathaway"},
    {"symbol": "JPM", "name": "JPMorgan Chase"},
    {"symbol": "V", "name": "Visa"},
    {"symbol": "MA", "name": "Mastercard"},
    {"symbol": "UNH", "name": "UnitedHealth"},
    {"symbol": "HD", "name": "Home Depot"},
    {"symbol": "PG", "name": "Procter & Gamble"},
    {"symbol": "JNJ", "name": "Johnson & Johnson"},
    {"symbol": "COST", "name": "Costco"},
    {"symbol": "WMT", "name": "Walmart"},
    {"symbol": "KO", "name": "Coca-Cola"},
    {"symbol": "PEP", "name": "PepsiCo"},
    {"symbol": "DIS", "name": "Disney"},
    {"symbol": "NFLX", "name": "Netflix"},
    {"symbol": "AMD", "name": "AMD"},
    {"symbol": "INTC", "name": "Intel"},
    {"symbol": "CRM", "name": "Salesforce"},
    {"symbol": "ADBE", "name": "Adobe"},
    {"symbol": "BAC", "name": "Bank of America"},
    {"symbol": "XOM", "name": "ExxonMobil"},
    {"symbol": "CVX", "name": "Chevron"},
    {"symbol": "PFE", "name": "Pfizer"},
    {"symbol": "T", "name": "AT&T"},
    {"symbol": "F", "name": "Ford"},
    {"symbol": "GM", "name": "General Motors"},
    {"symbol": "BA", "name": "Boeing"},
    {"symbol": "UBER", "name": "Uber"},
    {"symbol": "SBUX", "name": "Starbucks"},
    {"symbol": "PYPL", "name": "PayPal"},
    {"symbol": "QCOM", "name": "Qualcomm"},
    {"symbol": "ORCL", "name": "Oracle"},
    {"symbol": "SPY", "name": "S&P 500 ETF"},
    {"symbol": "QQQ", "name": "Nasdaq-100 ETF"},
    {"symbol": "VTI", "name": "Total Market ETF"},
]


def effective_watchlist(repos: Repositories, override_symbols: list[str] | None,
                        boot_watchlist: list[str]) -> list[str]:
    """Override wins; boot config is the fallback (the same order the scan
    cycle merges them)."""
    return list(override_symbols) if override_symbols else list(boot_watchlist)


def _card(repos: Repositories, symbol: str) -> dict[str, Any]:
    instrument = repos.instruments.get_by_symbol(symbol)
    if instrument is None:
        return {
            "symbol": symbol,
            "last_price": None,
            "change_abs": None,
            "change_pct": None,
            "is_gain": True,
            "chart_svg": interactive_line_chart([]),
            "has_data": False,
        }
    candles = repos.candles.get_recent(instrument.id, "1Day", _SPARK_DAYS)
    closes = [c.close for c in candles]
    if not closes:
        return {
            "symbol": symbol,
            "last_price": None,
            "change_abs": None,
            "change_pct": None,
            "is_gain": True,
            "chart_svg": interactive_line_chart([]),
            "has_data": False,
        }
    last = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else closes[0]
    change_abs = last - prev
    change_pct = (change_abs / prev) if prev else None
    labels = [c.ts.strftime("%b %d") for c in candles]
    gain = change_abs >= 0
    return {
        "symbol": symbol,
        "last_price": last,
        "change_abs": change_abs,
        "change_pct": change_pct,
        "is_gain": gain,
        "has_data": True,
        "chart_svg": interactive_line_chart(
            closes,
            labels,
            width=260,
            height=64,
            stroke="#1a7a34" if gain else "#b02a2a",
            value_prefix="$",
        ),
    }


def build_watchlist_view(
    repos: Repositories,
    override_symbols: list[str] | None,
    boot_watchlist: list[str],
) -> dict[str, Any]:
    symbols = effective_watchlist(repos, override_symbols, boot_watchlist)
    cards = [_card(repos, s) for s in symbols]
    # Merge curated + already-seen instrument symbols for the autocomplete,
    # de-duplicated, curated names preferred.
    seen = {t["symbol"] for t in COMMON_TICKERS}
    suggestions = list(COMMON_TICKERS)
    for s in symbols:
        if s not in seen:
            suggestions.append({"symbol": s, "name": ""})
            seen.add(s)
    return {
        "symbols": symbols,
        "cards": cards,
        "suggestions": sorted(suggestions, key=lambda t: t["symbol"]),
        "is_override": bool(override_symbols),
    }
