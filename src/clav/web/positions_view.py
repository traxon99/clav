"""Shared open-positions view (extracted from Story 4.5's /portfolio page so
the dashboard's "Open Positions" panel can show the same numbers): current
price, cost average, and unrealized P&L in both $ and % for every open
position. Current price is marked to the **last known close** from the
already-persisted ``candle`` table -- clav-web never calls the broker, so
this is "as of the last successful fetch," not a live quote."""

from __future__ import annotations

from typing import Any

from clav.data.repositories import Repositories


def build_position_rows(repos: Repositories) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in repos.positions.get_all():
        if row.qty == 0:
            continue
        instrument = repos.instruments.get_by_id(row.instrument_id)
        symbol = instrument.symbol if instrument is not None else ""
        latest_candles = (
            repos.candles.get_recent(row.instrument_id, "1Day", 1) if instrument else []
        )
        last_close = latest_candles[0].close if latest_candles else None
        unrealized_pl = (
            (last_close - row.avg_entry_price) * row.qty if last_close is not None else None
        )
        # Return on cost basis -- correctly signed for a short (qty < 0):
        # a price drop yields a positive unrealized_pl *and* a positive %.
        unrealized_pl_pct = (
            unrealized_pl / (abs(row.qty) * row.avg_entry_price)
            if unrealized_pl is not None and row.avg_entry_price
            else None
        )
        rows.append(
            {
                "symbol": symbol,
                "qty": row.qty,
                "avg_entry_price": row.avg_entry_price,
                "last_close": last_close,
                "unrealized_pl": unrealized_pl,
                "unrealized_pl_pct": unrealized_pl_pct,
                "stop_price": row.stop_price,
                "take_profit_price": row.take_profit_price,
            }
        )
    return rows
