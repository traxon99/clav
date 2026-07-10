from datetime import UTC, datetime

import pytest

from clav.clock import FakeClock
from clav.integrations.broker_factory import broker_factory
from clav.integrations.dryrun_broker import DryRunBroker
from clav.integrations.paper_broker import PaperBroker

NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


def test_paper_mode_returns_paper_broker() -> None:
    broker = broker_factory(
        "paper", clock=FakeClock(NOW), alpaca_api_key="key", alpaca_api_secret="secret"
    )
    assert isinstance(broker, PaperBroker)


def test_paper_mode_requires_credentials() -> None:
    with pytest.raises(ValueError, match="api_key"):
        broker_factory("paper", clock=FakeClock(NOW))


def test_dryrun_mode_returns_dryrun_broker() -> None:
    broker = broker_factory("dryrun", clock=FakeClock(NOW))
    assert isinstance(broker, DryRunBroker)


def test_live_mode_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="Epic 1"):
        broker_factory("live", clock=FakeClock(NOW))


def test_unknown_mode_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown broker mode"):
        broker_factory("bogus", clock=FakeClock(NOW))
