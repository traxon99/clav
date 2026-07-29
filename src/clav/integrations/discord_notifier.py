"""Discord webhook notifier for order executions -- separate from ``Alerter``
since executions aren't faults: every buy/sell should post, with no
dedup/digest/live-mode-escalation logic getting in the way.

Posts qty/price/cost, the LLM's conviction on the trade (``decision.reasoning``
carries whatever ``AnalystGateway`` attached, "n/a" for a technical-only
decision), and -- best-effort -- a sparkline of the symbol's last week so the
number means something without a chart app open."""

from __future__ import annotations

import json
from io import BytesIO

import httpx
from PIL import Image, ImageDraw

from clav.domain.models import Order, TradeDecision
from clav.interfaces.market_data import MarketDataSource

_BUY_COLOR = 0x1A7A34
_SELL_COLOR = 0xB02A2A
_CHART_SIZE = (600, 200)
_CHART_PAD = 16
_CHART_BG = (43, 45, 49)


class DiscordExecutionNotifier:
    def __init__(
        self,
        *,
        webhook_url: str,
        data_source: MarketDataSource | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._webhook_url = webhook_url
        self._data_source = data_source
        self._timeout = timeout

    def notify(self, order: Order, decision: TradeDecision) -> None:
        price = order.filled_avg_price or order.limit_price
        qty = order.filled_qty or order.qty
        cost = price * qty if price else None
        color = _BUY_COLOR if order.side == "buy" else _SELL_COLOR

        fields = [
            {"name": "Qty", "value": str(qty), "inline": True},
            {"name": "Price", "value": f"${price:.2f}" if price else "pending", "inline": True},
            {"name": "Cost", "value": f"${cost:,.2f}" if cost else "pending", "inline": True},
            {
                "name": "AI confidence",
                "value": _confidence_word(decision.reasoning.get("conviction")),
                "inline": True,
            },
        ]
        embed: dict[str, object] = {
            "title": f"{order.side.upper()} {order.symbol}",
            "color": color,
            "fields": fields,
        }

        chart = self._render_week_chart(order.symbol, color)
        payload = {"embeds": [embed]}
        with httpx.Client(timeout=self._timeout) as client:
            if chart is not None:
                embed["image"] = {"url": "attachment://chart.png"}
                resp = client.post(
                    self._webhook_url,
                    data={"payload_json": json.dumps(payload)},
                    files={"file": ("chart.png", chart, "image/png")},
                )
            else:
                resp = client.post(self._webhook_url, json=payload)
            resp.raise_for_status()

    def _render_week_chart(self, symbol: str, color: int) -> bytes | None:
        """None on any failure -- a missing/broken chart must never break the
        notification itself, same fail-open contract as the rest of this
        class's callers expect."""
        if self._data_source is None:
            return None
        try:
            candles = self._data_source.get_candles(symbol, "1Day", 7)
        except Exception:
            return None
        closes = [c.close for c in candles]
        if len(closes) < 2:
            return None
        rgb = ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)
        return _sparkline_png(closes, rgb)


def _confidence_word(conviction: object) -> str:
    if not isinstance(conviction, int | float):
        return "n/a"
    if conviction >= 0.66:
        return "High"
    if conviction >= 0.33:
        return "Medium"
    return "Low"


def _sparkline_png(closes: list[float], color: tuple[int, int, int]) -> bytes:
    width, height = _CHART_SIZE
    img = Image.new("RGB", (width, height), _CHART_BG)
    draw = ImageDraw.Draw(img)

    lo, hi = min(closes), max(closes)
    span = hi - lo or 1.0
    n = len(closes)
    plot_w, plot_h = width - 2 * _CHART_PAD, height - 2 * _CHART_PAD
    points = [
        (
            _CHART_PAD + i * plot_w / (n - 1),
            _CHART_PAD + plot_h - (c - lo) / span * plot_h,
        )
        for i, c in enumerate(closes)
    ]
    draw.line(points, fill=color, width=3, joint="curve")

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
