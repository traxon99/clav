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
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import TradingWindow, default_rules
from clav.integrations.alpaca_data import AlpacaDataAdapter
from clav.integrations.broker_factory import broker_factory
from clav.services.scan_cycle import ScanCycleService
from clav.services.scheduler import Scheduler

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

    return ScanCycleService(
        watchlist=cfg.watchlist,
        data_source=data_source,
        indicators=IndicatorService(),
        decision_engine=decision_engine,
        risk_engine=RiskEngine(default_rules()),
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
        mode=cfg.mode,
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
