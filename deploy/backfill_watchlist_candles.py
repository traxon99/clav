#!/usr/bin/env python3
"""One-off backfill: seed daily candles for every watchlist symbol via
Alpaca's own historical bars API, through the project's normal
MarketDataSource/AlpacaDataAdapter path (the exact same call scan_cycle.py
itself makes every cycle -- not a separate hand-rolled fetch).

Closes the same first-run gap as backfill_portfolio_history.py, for the
Home page's per-ticker sparklines and the technical-indicator pipeline
(both read candle rows, never Alpaca directly) instead of a real scan cycle:
a fresh database, or a market-hours-gated first run (docs/09-deployment.md
§9), leaves the watchlist showing "no price yet" / "not enough data yet"
until a cycle completes. This populates the same 200-bar window
scan_cycle's own default candle_limit uses, which comfortably covers both
the Home page's 30-day sparkline and any indicator lookback (ATR-14,
moving averages) -- so a scan cycle right afterward has real history to
compute against too, not just a chart to draw.

Idempotent: CandleRepository.upsert_many() already updates-in-place by
(instrument, timeframe, ts), so this is safe to re-run.

Usage (as the clav user, from /opt/clav):
    .venv/bin/python deploy/backfill_watchlist_candles.py
"""

from __future__ import annotations

import sys

from clav.clock import SystemClock
from clav.config import load_settings
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.integrations.alpaca_data import AlpacaDataAdapter

DEFAULT_TIMEFRAME = "1Day"
DEFAULT_LIMIT = 200  # matches ScanCycleService's own candle_limit default


def main() -> int:
    cfg = load_settings()
    if not cfg.alpaca.api_key or not cfg.alpaca.api_secret:
        print("No Alpaca keys configured -- nothing to backfill.", file=sys.stderr)
        return 1
    if not cfg.watchlist:
        print("No watchlist configured -- nothing to backfill.")
        return 0

    data_source = AlpacaDataAdapter(
        cfg.alpaca.api_key.get_secret_value(),
        cfg.alpaca.api_secret.get_secret_value(),
        clock=SystemClock(),
    )

    engine = make_engine(cfg.data_dir / "clav.db")
    session_factory = make_session_factory(engine)

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        for symbol in cfg.watchlist:
            try:
                candles = data_source.get_candles(symbol, DEFAULT_TIMEFRAME, DEFAULT_LIMIT)
            except Exception as exc:  # one bad symbol should not abort the rest
                print(f"{symbol}: fetch failed ({exc}); skipping", file=sys.stderr)
                continue
            if not candles:
                print(f"{symbol}: no candles returned; skipping")
                continue
            instrument = repos.instruments.get_or_create(
                symbol, sector=cfg.sector_map.get(symbol.upper())
            )
            repos.candles.upsert_many(instrument.id, candles)
            print(
                f"{symbol}: backfilled {len(candles)} candle(s), "
                f"{candles[0].ts.date()} -> {candles[-1].ts.date()}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
