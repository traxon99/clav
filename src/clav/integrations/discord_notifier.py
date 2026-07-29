"""Discord webhook notifier for order executions -- separate from ``Alerter``
since executions aren't faults: every buy/sell should post, with no
dedup/digest/live-mode-escalation logic getting in the way.

Posts qty/price/cost, the LLM's conviction on the trade (``decision.reasoning``
carries whatever ``AnalystGateway`` attached, "n/a" for a technical-only
decision), and -- best-effort -- a labeled chart of the symbol's last week so
the number means something without a chart app open."""

from __future__ import annotations

import json
from io import BytesIO

import httpx
from PIL import Image, ImageDraw, ImageFont

from clav.domain.models import Candle, Order, TradeDecision
from clav.interfaces.market_data import MarketDataSource

_BUY_COLOR = 0x1A7A34
_SELL_COLOR = 0xB02A2A
_CHART_SIZE = (640, 260)
_CHART_BG = (30, 32, 36)
_GRID_COLOR = (70, 72, 78)
_TEXT_COLOR = (170, 172, 178)
# Left margin fits a "$1,234.56" price label; bottom fits a "Jul 22" date label.
_MARGIN_LEFT = 60
_MARGIN_RIGHT = 16
_MARGIN_TOP = 14
_MARGIN_BOTTOM = 26
_FONT_SIZE = 13


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
        if len(candles) < 2:
            return None
        rgb = ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)
        return _week_chart_png(candles, rgb)


def _confidence_word(conviction: object) -> str:
    if not isinstance(conviction, int | float):
        return "n/a"
    if conviction >= 0.66:
        return "High"
    if conviction >= 0.33:
        return "Medium"
    return "Low"


def _week_chart_png(candles: list[Candle], color: tuple[int, int, int]) -> bytes:
    width, height = _CHART_SIZE
    img = Image.new("RGB", (width, height), _CHART_BG)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=_FONT_SIZE)

    closes = [c.close for c in candles]
    lo, hi = min(closes), max(closes)
    span = hi - lo or 1.0
    n = len(closes)
    plot_x0, plot_x1 = _MARGIN_LEFT, width - _MARGIN_RIGHT
    plot_y0, plot_y1 = _MARGIN_TOP, height - _MARGIN_BOTTOM
    plot_w, plot_h = plot_x1 - plot_x0, plot_y1 - plot_y0

    def x_at(i: int) -> float:
        return plot_x0 + i * plot_w / (n - 1)

    def y_at(price: float) -> float:
        return plot_y0 + plot_h - (price - lo) / span * plot_h

    # Gridlines + price labels at the low, mid, and high of the week's range
    # (not a zero-based axis -- the point is legibility of this week's move,
    # not comparability across symbols).
    for frac in (0.0, 0.5, 1.0):
        price = lo + frac * span
        y = y_at(price)
        draw.line([(plot_x0, y), (plot_x1, y)], fill=_GRID_COLOR, width=1)
        label = f"${price:,.2f}"
        _, top, _, bottom = draw.textbbox((0, 0), label, font=font)
        draw.text((4, y - (bottom - top) / 2 - top), label, fill=_TEXT_COLOR, font=font)

    # Date labels every other bar (covers both endpoints for the 7-bar week
    # this is always called with) -- labeling all 7 crowds them together.
    for i in range(0, n, 2):
        x = x_at(i)
        label = candles[i].ts.strftime("%b %d")
        left, _, right, _ = draw.textbbox((0, 0), label, font=font)
        draw.text((x - (right - left) / 2, plot_y1 + 6), label, fill=_TEXT_COLOR, font=font)

    points = [(x_at(i), y_at(c)) for i, c in enumerate(closes)]
    draw.line(points, fill=color, width=3, joint="curve")

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
