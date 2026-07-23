"""The Discover page's view model (autonomous-discovery epic): what the bot
surfaced on its own this cycle, the operator's pins, and recent "analyze this
ticker" requests. Read-only over already-persisted state — the discovery
snapshot in ``system_control``, the ``analysis_request`` queue, and the pins
(runtime watchlist). Reuses ``build_watchlist_view`` for the pin cards."""

from __future__ import annotations

import json
from typing import Any

from clav.data.repositories import Repositories
from clav.services.discovery import DISCOVERY_SNAPSHOT_KEY
from clav.web.watchlist_view import COMMON_TICKERS, build_watchlist_view


def _last_price(repos: Repositories, symbol: str) -> float | None:
    instrument = repos.instruments.get_by_symbol(symbol)
    if instrument is None:
        return None
    candles = repos.candles.get_recent(instrument.id, "1Day", 1)
    return candles[-1].close if candles else None


def _discovered(repos: Repositories) -> dict[str, Any]:
    empty: dict[str, Any] = {"generated_at": None, "rows": []}
    raw = repos.system_control.get(DISCOVERY_SNAPSHOT_KEY)
    if not raw:
        return empty
    try:
        snapshot = json.loads(raw)
    except (ValueError, TypeError):
        return empty
    rows: list[dict[str, Any]] = []
    for c in snapshot.get("candidates", []):
        symbol = c.get("symbol", "")
        rows.append(
            {
                "symbol": symbol,
                "score_pct": round(float(c.get("score", 0.0)) * 100),
                "mention_volume": c.get("mention_volume", 0),
                "anomaly_flag": bool(c.get("anomaly_flag", False)),
                "source": c.get("source", ""),
                "last_price": _last_price(repos, symbol),
            }
        )
    return {"generated_at": snapshot.get("generated_at"), "rows": rows}


def _requests(repos: Repositories) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in repos.analysis_requests.list_recent(limit=12):
        out.append(
            {
                "symbol": r.symbol,
                "status": r.status,
                "requested_at": r.requested_at,
                "decision_id": r.decision_id,
                "error": r.error,
            }
        )
    return out


def ticker_suggestions(repos: Repositories, query: str, *, limit: int = 12) -> list[dict[str, str]]:
    """Autocomplete over the cached Alpaca catalog when it's populated, else the
    curated fallback list (fresh install / no Alpaca key). Symbol + name."""
    q = query.strip().upper()
    if repos.assets.count() > 0:
        return [
            {"symbol": a.symbol, "name": a.name or ""}
            for a in repos.assets.search(query, limit=limit)
        ]
    if not q:
        return COMMON_TICKERS[:limit]
    return [t for t in COMMON_TICKERS if t["symbol"].startswith(q)][:limit]


def build_discover_view(
    repos: Repositories,
    override_watchlist: list[str] | None,
    boot_watchlist: list[str],
    *,
    discovery_enabled: bool,
    on_demand_enabled: bool,
) -> dict[str, Any]:
    return {
        "pins": build_watchlist_view(repos, override_watchlist, boot_watchlist),
        "discovered": _discovered(repos),
        "requests": _requests(repos),
        "discovery_enabled": discovery_enabled,
        "on_demand_enabled": on_demand_enabled,
        "catalog_size": repos.assets.count(),
    }
