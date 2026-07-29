"""DiscordExecutionNotifier: no live network in CI, httpx mocked at the
module boundary (same convention as test_alert_channels.py)."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from PIL import Image

from clav.domain.models import Candle, Order, TradeDecision
from clav.integrations.discord_notifier import DiscordExecutionNotifier
from clav.interfaces.market_data import MarketDataSource

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


def _decision(**overrides: object) -> TradeDecision:
    defaults: dict[str, object] = {
        "cycle_id": "cycle-1",
        "symbol": "AAPL",
        "action": "BUY",
        "target_qty": 8,
        "raw_score": 0.5,
        "technical_score": 0.5,
        "llm_signal": 0.4,
        "portfolio_bias": 0.0,
        "reasoning": {"conviction": 0.8},
    }
    defaults.update(overrides)
    return TradeDecision(**defaults)  # type: ignore[arg-type]


def _candles(closes: list[float]) -> list[Candle]:
    return [
        Candle(
            symbol="AAPL",
            timeframe="1Day",
            open=c,
            high=c,
            low=c,
            close=c,
            volume=1_000_000,
            ts=NOW,
        )
        for c in closes
    ]


def _data_source(closes: list[float] | None = None) -> MagicMock:
    source = MagicMock(spec=MarketDataSource)
    source.get_candles.return_value = _candles(closes) if closes is not None else []
    return source


def test_notify_posts_embed_with_symbol_qty_price_cost_and_confidence() -> None:
    with patch("clav.integrations.discord_notifier.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = MagicMock(raise_for_status=MagicMock())
        notifier = DiscordExecutionNotifier(webhook_url="https://discord.com/api/webhooks/x")
        notifier.notify(_order(), _decision())

    args, kwargs = client.post.call_args
    assert args[0] == "https://discord.com/api/webhooks/x"
    embed = kwargs["json"]["embeds"][0]
    assert embed["title"] == "BUY AAPL"
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields == {
        "Qty": "8",
        "Price": "$190.50",
        "Cost": "$1,524.00",
        "AI confidence": "High",
    }
    assert "Status" not in fields


def test_notify_without_a_fill_price_reports_pending_instead_of_crashing() -> None:
    with patch("clav.integrations.discord_notifier.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = MagicMock(raise_for_status=MagicMock())
        notifier = DiscordExecutionNotifier(webhook_url="https://discord.com/api/webhooks/x")
        order = _order(status="accepted", filled_qty=None, filled_avg_price=None)
        notifier.notify(order, _decision())

    embed = client.post.call_args[1]["json"]["embeds"][0]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Price"] == "pending"
    assert fields["Cost"] == "pending"


def test_notify_reports_no_analyst_conviction_as_not_rated() -> None:
    with patch("clav.integrations.discord_notifier.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = MagicMock(raise_for_status=MagicMock())
        notifier = DiscordExecutionNotifier(webhook_url="https://discord.com/api/webhooks/x")
        notifier.notify(_order(), _decision(reasoning={}))

    embed = client.post.call_args[1]["json"]["embeds"][0]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["AI confidence"] == "n/a"


def test_sell_uses_a_different_color_than_buy() -> None:
    with patch("clav.integrations.discord_notifier.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = MagicMock(raise_for_status=MagicMock())
        notifier = DiscordExecutionNotifier(webhook_url="https://discord.com/api/webhooks/x")

        notifier.notify(_order(side="buy"), _decision(action="BUY"))
        buy_color = client.post.call_args[1]["json"]["embeds"][0]["color"]

        notifier.notify(
            _order(side="sell", client_order_id="clav-cycle-1-AAPL-sell"),
            _decision(action="SELL"),
        )
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
            notifier.notify(_order(), _decision())
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the HTTP error to propagate")


def test_notify_with_a_data_source_attaches_a_week_chart() -> None:
    with patch("clav.integrations.discord_notifier.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = MagicMock(raise_for_status=MagicMock())
        source = _data_source([180.0, 182.0, 179.0, 185.0, 190.5])
        notifier = DiscordExecutionNotifier(
            webhook_url="https://discord.com/api/webhooks/x", data_source=source
        )
        notifier.notify(_order(), _decision())

    _, kwargs = client.post.call_args
    embed = json.loads(kwargs["data"]["payload_json"])["embeds"][0]
    assert embed["image"]["url"] == "attachment://chart.png"
    filename, content, content_type = kwargs["files"]["file"]
    assert filename == "chart.png"
    assert content_type == "image/png"
    Image.open(io.BytesIO(content)).verify()
    source.get_candles.assert_called_once_with("AAPL", "1Day", 7)


def test_notify_without_enough_candle_history_skips_the_chart() -> None:
    with patch("clav.integrations.discord_notifier.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = MagicMock(raise_for_status=MagicMock())
        source = _data_source([190.5])
        notifier = DiscordExecutionNotifier(
            webhook_url="https://discord.com/api/webhooks/x", data_source=source
        )
        notifier.notify(_order(), _decision())

    _, kwargs = client.post.call_args
    assert "json" in kwargs  # plain JSON post, no multipart file
    assert "image" not in kwargs["json"]["embeds"][0]


def test_notify_when_data_source_raises_skips_the_chart_without_failing() -> None:
    with patch("clav.integrations.discord_notifier.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = MagicMock(raise_for_status=MagicMock())
        source = MagicMock(spec=MarketDataSource)
        source.get_candles.side_effect = RuntimeError("data source down")
        notifier = DiscordExecutionNotifier(
            webhook_url="https://discord.com/api/webhooks/x", data_source=source
        )
        notifier.notify(_order(), _decision())

    _, kwargs = client.post.call_args
    assert "json" in kwargs
