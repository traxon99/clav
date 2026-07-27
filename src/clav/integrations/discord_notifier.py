"""Discord webhook notifier for order executions -- separate from ``Alerter``
since executions aren't faults: every buy/sell should post, with no
dedup/digest/live-mode-escalation logic getting in the way."""

from __future__ import annotations

import httpx

from clav.domain.models import Order

_BUY_COLOR = 0x1A7A34
_SELL_COLOR = 0xB02A2A


class DiscordExecutionNotifier:
    def __init__(self, *, webhook_url: str, timeout: float = 10.0) -> None:
        self._webhook_url = webhook_url
        self._timeout = timeout

    def notify(self, order: Order) -> None:
        price = order.filled_avg_price or order.limit_price
        embed = {
            "title": f"{order.side.upper()} {order.symbol}",
            "color": _BUY_COLOR if order.side == "buy" else _SELL_COLOR,
            "fields": [
                {"name": "Qty", "value": str(order.qty), "inline": True},
                {"name": "Status", "value": order.status, "inline": True},
                {"name": "Price", "value": f"${price:.2f}" if price else "n/a", "inline": True},
            ],
        }
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(self._webhook_url, json={"embeds": [embed]})
            resp.raise_for_status()
