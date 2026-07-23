"""A tiny inline-SVG line-chart helper (Story 4.5) — no charting library, no
CDN, no build step: the dashboard's charts are plain server-rendered ``<svg>``
markup (epic decision #1), so they render identically with JavaScript on or
off.

``interactive_line_chart`` adds the one dependency-free enhancement epic
decision #1 explicitly allows — a hover crosshair. The line still renders
server-side; a tiny vendored script in ``base.html`` reads the embedded
``data-points`` and shows the value under the cursor. With JS off you still get
the full line, just no tooltip.
"""

from __future__ import annotations

import html
import json

_PADDING = 4.0


def sparkline_svg(
    values: list[float],
    *,
    width: int = 640,
    height: int = 120,
    stroke: str = "#2a5db0",
    fill: str = "none",
) -> str:
    """A minimal line chart over ``values`` (left-to-right, oldest-first).
    Returns an empty-state SVG for 0-1 points rather than dividing by zero."""
    if len(values) < 2:
        return (
            f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'class="chart chart-empty" role="img" aria-label="not enough data">'
            f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
            f'font-size="12" fill="#999">not enough data yet</text></svg>'
        )

    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    plot_w = width - 2 * _PADDING
    plot_h = height - 2 * _PADDING
    step = plot_w / (len(values) - 1)

    points = []
    for i, v in enumerate(values):
        x = _PADDING + i * step
        y = _PADDING + plot_h * (1 - (v - lo) / span)
        points.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(points)

    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'class="chart" role="img" aria-label="line chart, {len(values)} points, '
        f'range {lo:.4g} to {hi:.4g}">'
        f'<polyline points="{polyline}" fill="{fill}" stroke="{stroke}" '
        f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" />'
        f"</svg>"
    )


def interactive_line_chart(
    values: list[float],
    labels: list[str] | None = None,
    *,
    width: int = 640,
    height: int = 160,
    stroke: str = "#1a7a34",
    value_prefix: str = "",
    value_suffix: str = "",
    fill: bool = True,
) -> str:
    """A line chart that reveals the value under the cursor on hover.

    ``values`` are plotted left-to-right (oldest first). ``labels`` (optional,
    same length) are shown alongside the value in the tooltip — typically a
    formatted timestamp, so "hover any graph to see the price" also answers
    *when*. The series is embedded as ``data-points`` (x-pixel + raw value) so
    the vendored hover script needs no server round-trip. Returns an
    empty-state SVG for 0-1 points rather than dividing by zero."""
    if len(values) < 2:
        return (
            f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'class="chart chart-empty" role="img" aria-label="not enough data">'
            f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
            f'font-size="12" fill="#999">not enough data yet</text></svg>'
        )

    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    plot_w = width - 2 * _PADDING
    plot_h = height - 2 * _PADDING
    step = plot_w / (len(values) - 1)

    xs: list[float] = []
    coords: list[str] = []
    for i, v in enumerate(values):
        x = _PADDING + i * step
        y = _PADDING + plot_h * (1 - (v - lo) / span)
        xs.append(x)
        coords.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(coords)

    area = ""
    if fill:
        baseline = height - _PADDING
        area_pts = f"{xs[0]:.1f},{baseline:.1f} " + polyline + f" {xs[-1]:.1f},{baseline:.1f}"
        area = (
            f'<polygon points="{area_pts}" fill="{stroke}" fill-opacity="0.08" '
            f'stroke="none" />'
        )

    # x-pixel + raw value per point, plus optional label, for the hover script.
    points_json = json.dumps(
        [
            {"x": round(xs[i], 1), "v": values[i], "l": (labels[i] if labels else None)}
            for i in range(len(values))
        ],
        separators=(",", ":"),
    )
    data_attr = html.escape(points_json, quote=True)

    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'class="chart chart-interactive" role="img" '
        f'preserveAspectRatio="none" '
        f'data-points="{data_attr}" data-prefix="{html.escape(value_prefix, quote=True)}" '
        f'data-suffix="{html.escape(value_suffix, quote=True)}" '
        f'aria-label="line chart, {len(values)} points, '
        f'range {lo:.4g} to {hi:.4g}">'
        f"{area}"
        f'<polyline points="{polyline}" fill="none" stroke="{stroke}" '
        f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" '
        f'vector-effect="non-scaling-stroke" />'
        f'<line class="chart-crosshair" x1="0" y1="{_PADDING}" x2="0" y2="{height - _PADDING}" '
        f'stroke="currentColor" stroke-width="1" stroke-dasharray="3 3" '
        f'opacity="0" pointer-events="none" />'
        f'<circle class="chart-dot" r="3.5" fill="{stroke}" stroke="var(--surface)" '
        f'stroke-width="1.5" opacity="0" pointer-events="none" />'
        f'<rect class="chart-hit" x="0" y="0" width="{width}" height="{height}" '
        f'fill="transparent" pointer-events="all" />'
        f"</svg>"
    )


def scatter_svg(
    points: list[tuple[float, float]],
    *,
    width: int = 640,
    height: int = 280,
    win_color: str = "#1a7a34",
    loss_color: str = "#b02a2a",
) -> str:
    """A minimal (x, y) scatter chart (Story 4.9's conviction-vs-realized-P&L
    calibration view). Each point is colored win/loss (y >= 0 / y < 0) using
    the dashboard's existing action-BUY/action-SELL palette, with a dashed
    zero-line for the P&L break-even. Returns an empty-state SVG for 0
    points rather than dividing by zero."""
    if not points:
        return (
            f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'class="chart chart-empty" role="img" aria-label="not enough data">'
            f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
            f'font-size="12" fill="#999">not enough data yet</text></svg>'
        )

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_lo, x_hi = min(xs), max(xs)
    y_lo, y_hi = min(min(ys), 0.0), max(max(ys), 0.0)
    x_span = (x_hi - x_lo) or 1.0
    y_span = (y_hi - y_lo) or 1.0
    plot_w = width - 2 * _PADDING
    plot_h = height - 2 * _PADDING

    def _x(v: float) -> float:
        return _PADDING + plot_w * (v - x_lo) / x_span

    def _y(v: float) -> float:
        return _PADDING + plot_h * (1 - (v - y_lo) / y_span)

    zero_y = _y(0.0)
    circles = "".join(
        f'<circle cx="{_x(x):.1f}" cy="{_y(y):.1f}" r="3" '
        f'fill="{win_color if y >= 0 else loss_color}" />'
        for x, y in points
    )

    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'class="chart" role="img" aria-label="scatter chart, {len(points)} points">'
        f'<line x1="{_PADDING}" y1="{zero_y:.1f}" x2="{width - _PADDING}" y2="{zero_y:.1f}" '
        f'stroke="#ccc" stroke-width="1" stroke-dasharray="4 3" />'
        f"{circles}</svg>"
    )
