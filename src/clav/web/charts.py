"""A tiny inline-SVG line-chart helper (Story 4.5) — no charting library, no
CDN, no JS: the dashboard's charts are plain server-rendered ``<svg>`` markup
(epic decision #1), so they render identically with JavaScript on or off.
"""

from __future__ import annotations

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
