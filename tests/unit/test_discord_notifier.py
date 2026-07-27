"""DiscordExecutionNotifier: no live network in CI, httpx mocked at the
module boundary (same convention as test_alert_channels.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from clav.domain.models import Order
from clav.integrations.discord_notifier import DiscordExecutionNotifier

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def _order(**overrides: object) -> Order:
    defaults: dict[str, object] = {
        "client_order_id": "clav-cycle-1-AAPL-buy",
        "broker_order_id": "broker-1",
        "symbol": "AAPL",
        "side": "buy",
        "qty": 8,
        "status": "filled",
        "submitted_at": NOW,
        "updated_at": NOW,
        "filled_qty": 8,
        "filled_avg_price": 190.5,
    }
    defaults.update(overrides)
    return Order(**defaults)  # type: ignore[arg-type]


def test_notify_posts_embed_with_symbol_qty_and_price() -> None:
    with patch("clav.integrations.discord_notifier.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = MagicMock(raise_for_status=MagicMock())
        notifier = DiscordExecutionNotifier(webhook_url="https://discord.com/api/webhooks/x")
        notifier.notify(_order())

    args, kwargs = client.post.call_args
    assert args[0] == "https://discord.com/api/webhooks/x"
    embed = kwargs["json"]["embeds"][0]
    assert embed["title"] == "BUY AAPL"
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields == {"Qty": "8", "Status": "filled", "Price": "$190.50"}


def test_sell_uses_a_different_color_than_buy() -> None:
    with patch("clav.integrations.discord_notifier.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = MagicMock(raise_for_status=MagicMock())
        notifier = DiscordExecutionNotifier(webhook_url="https://discord.com/api/webhooks/x")

        notifier.notify(_order(side="buy"))
        buy_color = client.post.call_args[1]["json"]["embeds"][0]["color"]

        notifier.notify(_order(side="sell", client_order_id="clav-cycle-1-AAPL-sell"))
        sell_color = client.post.call_args[1]["json"]["embeds"][0]["color"]

    assert buy_color != sell_color


def test_notify_raises_on_http_error() -> None:
    with patch("clav.integrations.discord_notifier.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        response = MagicMock()
        response.raise_for_status.side_effect = RuntimeError("500 server error")
        client.post.return_value = response
        notifier = DiscordExecutionNotifier(webhook_url="https://discord.com/api/webhooks/x")

        try:
            notifier.notify(_order())
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the HTTP error to propagate")
