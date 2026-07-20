"""PortfolioManager — single source of truth for account state (Story 1.12,
docs/02-modules.md §7). Everything else reads a ``PortfolioSnapshot`` from here.

Placed in ``domain/`` per docs/08-project-structure.md, even though — like the
module diagram in docs/01-architecture.md shows (``PM --> DB``, driven by
``reconcile(broker)``) — it depends on the repository layer and the ``Broker``
interface. That's why the import-linter contract only forbids
domain/interfaces -> integrations (the one rule the docs actually state), not
domain -> data/interfaces generally.

The broker is authoritative for shares/cash (docs/02-modules.md §7); this
class treats ``reconcile(broker)`` as the only way position/account state
enters the system, and ``apply_fill`` narrows the position/trade delta a
single fill represents.

Exposure/unrealized-P&L (Story 2.2, docs/epics/epic-02-risk-and-portfolio.md)
use the broker's reported ``market_value``/``unrealized_pl`` per position
when available (the broker is authoritative, so we don't add a second live
-quote source just for this) and fall back to cost basis (qty x avg entry
price) only when the broker hasn't supplied a market value — e.g. a
plain ``snapshot()`` read between reconciles, or in tests. Peak equity is
the running max of every persisted snapshot's equity, so drawdown is
`(peak - equity) / peak`; ``daily_reset`` rebases that peak to the current
equity once a day so a single all-time high doesn't permanently mute the
drawdown rule.
"""

from __future__ import annotations

from datetime import datetime

from clav.clock import Clock
from clav.common.logging import get_logger
from clav.data import tables
from clav.data.repositories import Repositories
from clav.domain.models import Fill, PortfolioSnapshot, Position
from clav.interfaces.broker import Broker


class PortfolioManager:
    def __init__(self, repos: Repositories, *, clock: Clock) -> None:
        self._repos = repos
        self._clock = clock
        self._logger = get_logger(__name__)
        self._cached_account: dict[str, float] | None = None
        self._reconciled = False

    def apply_fill(self, fill: Fill) -> None:
        """Update position/trade state from one fill. Opens a new position/trade
        on the first buy, adds to an existing position on a subsequent buy
        (weighted-average entry price), and shrinks/closes on a sell."""
        order_row = self._repos.orders.get_by_client_order_id(fill.client_order_id)
        if order_row is None:
            raise ValueError(
                f"apply_fill: no order found for client_order_id={fill.client_order_id!r}"
            )
        instrument = self._repos.instruments.get_by_id(order_row.instrument_id)
        if instrument is None:
            raise ValueError(f"apply_fill: unknown instrument_id={order_row.instrument_id!r}")

        now = self._clock.now()
        existing_row = self._repos.positions.get(instrument.id)

        if order_row.side == "buy":
            self._apply_buy(instrument, order_row, existing_row, fill, now)
        else:
            self._apply_sell(instrument, order_row, existing_row, fill, now)

    def _apply_buy(
        self,
        instrument: tables.Instrument,
        order_row: tables.Order,
        existing_row: tables.Position | None,
        fill: Fill,
        now: datetime,
    ) -> None:
        if existing_row is None or existing_row.qty <= 0:
            position = Position(symbol=instrument.symbol, qty=fill.qty, avg_entry_price=fill.price)
            self._repos.positions.upsert(instrument.id, position, opened_at=now)
            self._repos.trades.open_trade(
                instrument_id=instrument.id,
                entry_order_id=order_row.id,
                entry_decision_id=order_row.decision_id,
                qty=fill.qty,
                entry_price=fill.price,
                opened_at=now,
            )
            return

        total_qty = existing_row.qty + fill.qty
        new_avg = (
            existing_row.avg_entry_price * existing_row.qty + fill.price * fill.qty
        ) / total_qty
        position = Position(
            symbol=instrument.symbol,
            qty=total_qty,
            avg_entry_price=new_avg,
            stop_price=existing_row.stop_price,
            take_profit_price=existing_row.take_profit_price,
        )
        self._repos.positions.upsert(instrument.id, position, opened_at=existing_row.opened_at)

        open_trade = self._repos.trades.get_open_trade(instrument.id)
        if open_trade is not None:
            open_trade.qty = total_qty
            open_trade.entry_price = new_avg

    def _apply_sell(
        self,
        instrument: tables.Instrument,
        order_row: tables.Order,
        existing_row: tables.Position | None,
        fill: Fill,
        now: datetime,
    ) -> None:
        if existing_row is None or existing_row.qty <= 0:
            self._logger.warning(
                "apply_fill_sell_with_no_open_position",
                symbol=instrument.symbol,
                client_order_id=fill.client_order_id,
            )
            return

        open_trade = self._repos.trades.get_open_trade(instrument.id)
        remaining_qty = existing_row.qty - fill.qty

        if remaining_qty > 0:
            position = Position(
                symbol=instrument.symbol,
                qty=remaining_qty,
                avg_entry_price=existing_row.avg_entry_price,
                stop_price=existing_row.stop_price,
                take_profit_price=existing_row.take_profit_price,
            )
            self._repos.positions.upsert(instrument.id, position, opened_at=existing_row.opened_at)
            if open_trade is not None:
                partial_pl = (fill.price - open_trade.entry_price) * fill.qty
                open_trade.realized_pl = (open_trade.realized_pl or 0.0) + partial_pl
                open_trade.qty = remaining_qty
            return

        self._repos.positions.delete(instrument.id)
        if open_trade is not None:
            realized_pl = (open_trade.realized_pl or 0.0) + (
                fill.price - open_trade.entry_price
            ) * fill.qty
            # NOTE: return_pct is relative to the entry cost of the remaining
            # (pre-this-fill) tranche, not the trade's original total size —
            # Epic 1's Trade schema tracks remaining qty, not original lot
            # history. Full lot accounting is future work.
            cost_basis = open_trade.entry_price * open_trade.qty
            return_pct = realized_pl / cost_basis if cost_basis else 0.0
            self._repos.trades.close_trade(
                open_trade.id,
                exit_order_id=order_row.id,
                exit_price=fill.price,
                closed_at=now,
                realized_pl=realized_pl,
                return_pct=return_pct,
            )

    def reconcile(self, broker: Broker) -> PortfolioSnapshot:
        """Pull authoritative positions/account from the broker and sync the
        DB. Must run before any new decision (docs/02-modules.md §7); on
        failure the returned/persisted snapshot is marked unreconciled so
        guardrails can freeze new entries."""
        try:
            account = broker.get_account()
            broker_positions = broker.get_positions()
        except Exception as exc:
            self._logger.error("portfolio_reconcile_failed", error=str(exc))
            self._reconciled = False
            snap = self.snapshot()
            self._repos.portfolio_snapshots.add(snap)
            return snap

        self._cached_account = {
            "cash": account.cash,
            "buying_power": account.buying_power,
            "equity": account.equity,
        }
        self._reconciled = True

        broker_symbols = {p.symbol for p in broker_positions}
        for p in broker_positions:
            instrument = self._repos.instruments.get_or_create(p.symbol)
            self._repos.positions.upsert(instrument.id, p, opened_at=self._clock.now())

        for row in self._repos.positions.get_all():
            row_instrument = self._repos.instruments.get_by_id(row.instrument_id)
            if row_instrument is not None and row_instrument.symbol not in broker_symbols:
                self._repos.positions.delete(row.instrument_id)

        snap = self._build_snapshot(list(broker_positions))
        self._repos.portfolio_snapshots.add(snap)
        return snap

    def snapshot(self) -> PortfolioSnapshot:
        return self._build_snapshot(self._load_positions_from_db())

    def daily_reset(self, broker: Broker) -> PortfolioSnapshot:
        """Rebase peak equity to today's equity and reset the daily-loss
        baseline (Story 2.2). ``MaxDailyLossRule`` (Story 2.5) reads
        ``daily_start_equity`` back out of ``system_control``."""
        snap = self.reconcile(broker)
        if not snap.reconciled:
            self._logger.warning("daily_reset_skipped_unreconciled")
            return snap

        rebased = snap.model_copy(update={"peak_equity": snap.equity, "drawdown": 0.0})
        self._repos.portfolio_snapshots.add(rebased)
        now = self._clock.now()
        self._repos.system_control.set(
            "daily_start_equity", str(snap.equity), updated_at=now, updated_by="system"
        )
        return rebased

    def _load_positions_from_db(self) -> list[Position]:
        positions: list[Position] = []
        for row in self._repos.positions.get_all():
            instrument = self._repos.instruments.get_by_id(row.instrument_id)
            symbol = instrument.symbol if instrument is not None else ""
            positions.append(
                Position(
                    symbol=symbol,
                    qty=row.qty,
                    avg_entry_price=row.avg_entry_price,
                    stop_price=row.stop_price,
                    take_profit_price=row.take_profit_price,
                )
            )
        return positions

    def _build_snapshot(self, positions: list[Position]) -> PortfolioSnapshot:
        now = self._clock.now()
        gross_exposure = 0.0
        net_exposure = 0.0
        unrealized_pl = 0.0
        sector_allocation: dict[str, float] = {}

        for p in positions:
            cost_basis = p.qty * p.avg_entry_price
            market_value = p.market_value if p.market_value is not None else cost_basis
            pos_unrealized_pl = (
                p.unrealized_pl if p.unrealized_pl is not None else market_value - cost_basis
            )
            gross_exposure += abs(market_value)
            net_exposure += market_value
            unrealized_pl += pos_unrealized_pl

            instrument = self._repos.instruments.get_by_symbol(p.symbol)
            sector = (
                instrument.sector if instrument is not None and instrument.sector else "unknown"
            )
            sector_allocation[sector] = sector_allocation.get(sector, 0.0) + market_value

        account = self._cached_account
        equity = account["equity"] if account else 0.0
        prior_snapshot = self._repos.portfolio_snapshots.latest()
        prior_peak_equity = prior_snapshot.peak_equity if prior_snapshot is not None else 0.0
        peak_equity = max(prior_peak_equity, equity)
        drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0

        return PortfolioSnapshot(
            ts=now,
            cash=account["cash"] if account else 0.0,
            equity=equity,
            buying_power=account["buying_power"] if account else 0.0,
            positions=positions,
            unrealized_pl=unrealized_pl,
            gross_exposure=gross_exposure,
            net_exposure=net_exposure,
            drawdown=drawdown,
            peak_equity=peak_equity,
            sector_allocation=sector_allocation,
            reconciled=self._reconciled,
        )
