"""broker_factory(mode) — the only place that chooses which Broker
implementation to construct (docs/05-class-design.md §2, Story 1.6)."""

from __future__ import annotations

from clav.clock import Clock
from clav.integrations.dryrun_broker import DryRunBroker
from clav.integrations.paper_broker import PaperBroker
from clav.interfaces.broker import Broker


def broker_factory(
    mode: str,
    *,
    clock: Clock,
    alpaca_api_key: str | None = None,
    alpaca_api_secret: str | None = None,
) -> Broker:
    if mode == "paper":
        if not alpaca_api_key or not alpaca_api_secret:
            raise ValueError("paper mode requires alpaca_api_key and alpaca_api_secret")
        return PaperBroker(alpaca_api_key, alpaca_api_secret)
    if mode == "dryrun":
        return DryRunBroker(clock=clock)
    if mode == "live":
        raise NotImplementedError(
            "live trading is not implemented in Epic 1 (see docs/epics/epic-01-foundation.md)"
        )
    raise ValueError(f"unknown broker mode: {mode!r}")
