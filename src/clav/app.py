"""Composition root & CLI entrypoint for clav-core (docs/05-class-design.md §4).

Assembles the entire scan-cycle pipeline from config in one place — nothing
deep in the tree constructs its own dependencies, which is what keeps CLAV
testable and swappable (paper/dryrun via ``broker_factory``).
"""

from __future__ import annotations

import time

import click

from clav.clock import Clock, SystemClock
from clav.common.errors import ConfigError
from clav.common.logging import configure_logging, get_logger
from clav.config import Settings, load_settings
from clav.data.db import make_engine, make_session_factory
from clav.domain.decision import DecisionEngine, Thresholds, Weights
from clav.domain.indicators import IndicatorService
from clav.domain.models import EarningsEvent
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import TradingWindow, default_rules
from clav.domain.risk.sizing import PositionSizer
from clav.integrations.alpaca_data import AlpacaDataAdapter
from clav.integrations.broker_factory import broker_factory
from clav.services.scan_cycle import ScanCycleService
from clav.services.scheduler import Scheduler
from clav.services.stop_monitor import StopMonitor

_logger = get_logger(__name__)


def build_scan_cycle_service(cfg: Settings, *, clock: Clock | None = None) -> ScanCycleService:
    clock = clock or SystemClock()

    engine = make_engine(cfg.data_dir / "clav.db")
    session_factory = make_session_factory(engine)

    api_key = cfg.alpaca.api_key.get_secret_value()
    api_secret = cfg.alpaca.api_secret.get_secret_value()

    broker = broker_factory(
        cfg.mode, clock=clock, alpaca_api_key=api_key, alpaca_api_secret=api_secret
    )
    data_source = AlpacaDataAdapter(api_key, api_secret, clock=clock)

    decision_engine = DecisionEngine(
        Weights(
            technical=cfg.weights.technical, llm=cfg.weights.llm, portfolio=cfg.weights.portfolio
        ),
        Thresholds(buy=cfg.thresholds.buy, sell=cfg.thresholds.sell),
        default_order_value=cfg.risk.default_order_value,
        clock=clock,
    )
    position_sizer = PositionSizer(
        risk_fraction=cfg.risk.risk_fraction,
        atr_stop_mult=cfg.risk.atr_stop_mult,
        take_profit_mult=cfg.risk.take_profit_mult,
        default_order_value=cfg.risk.default_order_value,
    )
    stop_monitor = StopMonitor(
        data_source, clock=clock, quote_staleness_seconds=cfg.risk.quote_staleness_seconds
    )
    earnings_calendar = [
        EarningsEvent(
            symbol=entry.symbol,
            event_type=entry.event_type,
            scheduled_at=entry.scheduled_at,
            confirmed=entry.confirmed,
            source=entry.source,
        )
        for entry in cfg.earnings_calendar
    ]

    return ScanCycleService(
        watchlist=cfg.watchlist,
        data_source=data_source,
        indicators=IndicatorService(),
        decision_engine=decision_engine,
        risk_engine=RiskEngine(default_rules()),
        position_sizer=position_sizer,
        stop_monitor=stop_monitor,
        broker=broker,
        session_factory=session_factory,
        clock=clock,
        trading_window=TradingWindow(
            start=cfg.trading_window.start,
            end=cfg.trading_window.end,
            timezone=cfg.trading_window.timezone,
        ),
        max_position_value=cfg.risk.max_position_value,
        buying_power_buffer_pct=cfg.risk.buying_power_buffer_pct,
        max_portfolio_exposure_pct=cfg.risk.max_portfolio_exposure_pct,
        max_sector_allocation_pct=cfg.risk.max_sector_allocation_pct,
        max_daily_loss_pct=cfg.risk.max_daily_loss_pct,
        max_drawdown_pct=cfg.risk.max_drawdown_pct,
        min_avg_volume=cfg.risk.min_avg_volume,
        earnings_blackout_days=cfg.risk.earnings_blackout_days,
        cooldown_minutes=cfg.risk.cooldown_minutes,
        post_loss_cooldown_minutes=cfg.risk.post_loss_cooldown_minutes,
        mode=cfg.mode,
        sector_map=cfg.sector_map,
        earnings_calendar=earnings_calendar,
    )


def run_core() -> None:
    try:
        cfg = load_settings()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error:\n{exc}") from None

    configure_logging(log_dir=cfg.log_dir)
    _logger.info("clav_core_starting", mode=cfg.mode, watchlist=cfg.watchlist)

    service = build_scan_cycle_service(cfg)
    scheduler = Scheduler(service, scan_interval_minutes=cfg.scan_interval_minutes)
    scheduler.start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        _logger.info("clav_core_shutting_down")
        scheduler.shutdown()


@click.group()
def cli() -> None:
    """CLAV core entrypoints."""


@cli.command()
def core() -> None:
    """Run the clav-core trading daemon (systemd ExecStart target)."""
    run_core()


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
