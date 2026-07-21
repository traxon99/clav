"""GET /api/positions — read-only positions/P&L summary (Story 3.8), from the
latest ``portfolio_snapshot`` + the ``position`` table. Context for judging
decisions in the journal, not the interactive charting dashboard (that's
Epic 4)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from clav.data.repositories import Repositories
from clav.web.deps import get_repos

router = APIRouter(prefix="/api", tags=["positions"])


@router.get("/positions")
def get_positions(repos: Repositories = Depends(get_repos)) -> dict[str, Any]:
    snapshot = repos.portfolio_snapshots.latest()
    positions = []
    for row in repos.positions.get_all():
        if row.qty == 0:
            continue
        instrument = repos.instruments.get_by_id(row.instrument_id)
        positions.append(
            {
                "symbol": instrument.symbol if instrument is not None else "",
                "qty": row.qty,
                "avg_entry_price": row.avg_entry_price,
                "stop_price": row.stop_price,
                "take_profit_price": row.take_profit_price,
            }
        )

    return {
        "snapshot": (
            {
                "ts": snapshot.ts.isoformat(),
                "cash": snapshot.cash,
                "equity": snapshot.equity,
                "buying_power": snapshot.buying_power,
                "unrealized_pl": snapshot.unrealized_pl,
                "realized_pl": snapshot.realized_pl,
                "gross_exposure": snapshot.gross_exposure,
                "net_exposure": snapshot.net_exposure,
                "drawdown": snapshot.drawdown,
                "peak_equity": snapshot.peak_equity,
                "sector_allocation": snapshot.sector_allocation,
                "reconciled": snapshot.reconciled,
            }
            if snapshot is not None
            else None
        ),
        "positions": positions,
    }
