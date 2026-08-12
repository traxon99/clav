#!/usr/bin/env python3
"""One-off backfill: seed portfolio_snapshot from Alpaca's own portfolio-
history API.

clav-web's equity chart (web/portfolio_value.py) is purely descriptive over
already-persisted portfolio_snapshot rows -- it never queries Alpaca's own
account history itself (see docs/09-deployment.md §7). That's fine for a
long-running install, but it means a fresh database starts the chart
completely flat even when the underlying Alpaca account has real prior
history (e.g. reusing paper keys from an earlier install). This script
closes that gap once, by importing that real history as snapshot rows.

peak_equity/drawdown are computed as a running max/drawdown over the
imported series in chronological order, matching PortfolioManager's own
formula (domain/portfolio.py) -- these feed MaxDrawdownRule directly, so
getting them right here (not just cosmetically populating the chart)
matters: the live system's next real snapshot reads the *latest* row's
peak_equity as its own starting baseline.

Idempotent: skips any timestamp that already has a row, so it's safe to
re-run later (e.g. after a real outage gap) without duplicating history.

Usage (as the clav user, from /opt/clav):
    .venv/bin/python deploy/backfill_portfolio_history.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from alpaca.trading.client import TradingClient
from alpaca.trading.models import PortfolioHistory
from alpaca.trading.requests import GetPortfolioHistoryRequest
from sqlalchemy import select

from clav.config import load_settings
from clav.data import tables
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.domain.models import PortfolioSnapshot


def _fetch_points(client: TradingClient, *, period: str, timeframe: str) -> dict[int, float]:
    history = client.get_portfolio_history(
        GetPortfolioHistoryRequest(period=period, timeframe=timeframe, extended_hours=True)
    )
    if not isinstance(history, PortfolioHistory):
        raise TypeError(f"unexpected response type from get_portfolio_history: {type(history)}")
    return {
        int(ts): float(equity)
        for ts, equity in zip(history.timestamp, history.equity, strict=True)
        if equity is not None
    }


def main() -> int:
    cfg = load_settings()
    if not cfg.alpaca.api_key or not cfg.alpaca.api_secret:
        print("No Alpaca keys configured -- nothing to backfill.", file=sys.stderr)
        return 1

    client = TradingClient(
        cfg.alpaca.api_key.get_secret_value(),
        cfg.alpaca.api_secret.get_secret_value(),
        paper=True,
    )

    # A fine-grained recent week (good for the 1H/1D/1W chart tabs) merged
    # with a coarser full year (good for YTD/1Y) -- Alpaca only allows fine
    # granularity over short periods. Where they overlap, the finer set wins.
    daily = _fetch_points(client, period="1A", timeframe="1D")
    fine = _fetch_points(client, period="1W", timeframe="15Min")
    merged = {**daily, **fine}

    if not merged:
        print("Alpaca returned no portfolio history -- nothing to backfill.")
        return 0

    points = sorted((datetime.fromtimestamp(ts, tz=UTC), equity) for ts, equity in merged.items())

    engine = make_engine(cfg.data_dir / "clav.db")
    session_factory = make_session_factory(engine)

    with session_scope(session_factory) as session:
        existing = set(session.execute(select(tables.PortfolioSnapshot.ts)).scalars().all())
        repos = Repositories(session)

        inserted = 0
        skipped = 0
        peak_equity = 0.0
        for ts, equity in points:
            if ts in existing:
                skipped += 1
                continue
            peak_equity = max(peak_equity, equity)
            drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
            repos.portfolio_snapshots.add(
                PortfolioSnapshot(
                    ts=ts,
                    cash=0.0,
                    equity=equity,
                    buying_power=0.0,
                    unrealized_pl=0.0,
                    realized_pl=0.0,
                    gross_exposure=0.0,
                    net_exposure=0.0,
                    drawdown=drawdown,
                    peak_equity=peak_equity,
                    sector_allocation={},
                    reconciled=True,
                )
            )
            inserted += 1

    print(f"Backfilled {inserted} snapshot(s) from Alpaca history, skipped {skipped} existing.")
    if inserted:
        first, last = points[0][0], points[-1][0]
        print(f"Range: {first.isoformat()} -> {last.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
