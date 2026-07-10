from datetime import UTC, datetime

import pytest

from clav.clock import FakeClock
from clav.domain.models import OrderRequest
from clav.integrations.dryrun_broker import DryRunBroker

NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


def test_submit_order_logs_intent_and_returns_accepted_order_without_network() -> None:
    broker = DryRunBroker(clock=FakeClock(NOW))
    req = OrderRequest(client_order_id="clav-c1-AAPL-buy", symbol="AAPL", side="buy", qty=8)

    order = broker.submit_order(req)

    assert order.status == "accepted"
    assert order.client_order_id == "clav-c1-AAPL-buy"
    assert broker.submitted_orders == [req]


def test_submit_order_is_idempotent_on_client_order_id() -> None:
    broker = DryRunBroker(clock=FakeClock(NOW))
    req = OrderRequest(client_order_id="clav-c1-AAPL-buy", symbol="AAPL", side="buy", qty=8)

    first = broker.submit_order(req)
    second = broker.submit_order(req)

    assert first == second
    assert len(broker.submitted_orders) == 1  # not double-logged


def test_get_order_returns_none_for_unknown_client_order_id() -> None:
    broker = DryRunBroker(clock=FakeClock(NOW))
    assert broker.get_order("nope") is None


def test_cancel_order_marks_canceled() -> None:
    broker = DryRunBroker(clock=FakeClock(NOW))
    req = OrderRequest(client_order_id="clav-c1-AAPL-buy", symbol="AAPL", side="buy", qty=8)
    broker.submit_order(req)

    broker.cancel_order("clav-c1-AAPL-buy")

    assert broker.get_order("clav-c1-AAPL-buy").status == "canceled"


def test_cancel_order_unknown_is_noop() -> None:
    broker = DryRunBroker(clock=FakeClock(NOW))
    broker.cancel_order("does-not-exist")  # should not raise


def test_get_account_defaults_are_sane() -> None:
    broker = DryRunBroker(clock=FakeClock(NOW))
    account = broker.get_account()
    assert account.cash > 0
    assert account.buying_power > 0


def test_get_positions_defaults_to_empty() -> None:
    broker = DryRunBroker(clock=FakeClock(NOW))
    assert broker.get_positions() == []


@pytest.mark.parametrize("market_open", [True, False])
def test_get_clock_reflects_configured_market_open(market_open) -> None:
    broker = DryRunBroker(clock=FakeClock(NOW), market_open=market_open)
    assert broker.get_clock().is_open is market_open


def test_get_clock_uses_injected_clock_not_wallclock() -> None:
    broker = DryRunBroker(clock=FakeClock(NOW))
    assert broker.get_clock().timestamp == NOW
