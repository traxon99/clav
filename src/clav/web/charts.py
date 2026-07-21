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
