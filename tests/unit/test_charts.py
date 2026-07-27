"""Story 4.5 — sparkline_svg: a tiny inline-SVG line-chart helper. No JS,
no CDN; must degrade gracefully on 0/1-point series instead of dividing by
zero, and produce valid, parseable SVG for a real series."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from clav.web.charts import interactive_line_chart, scatter_svg, sparkline_svg


def _parse(svg_text: str) -> ET.Element:
    return ET.fromstring(svg_text)


def test_empty_series_renders_placeholder_svg() -> None:
    svg = sparkline_svg([])
    root = _parse(svg)
    assert root.tag.endswith("svg")
    assert "chart-empty" in svg


def test_single_point_renders_placeholder_svg() -> None:
    svg = sparkline_svg([42.0])
    assert "chart-empty" in svg
    _parse(svg)  # must still be valid XML


def test_two_points_renders_a_polyline() -> None:
    svg = sparkline_svg([1.0, 2.0])
    root = _parse(svg)
    polyline = root.find("polyline")
    assert polyline is not None
    points = polyline.get("points")
    assert points is not None
    assert len(points.split()) == 2


def test_flat_series_does_not_divide_by_zero(tmp_path=None) -> None:
    svg = sparkline_svg([5.0, 5.0, 5.0])
    root = _parse(svg)
    polyline = root.find("polyline")
    assert polyline is not None
    assert len(polyline.get("points").split()) == 3


def test_max_value_does_not_touch_top_edge() -> None:
    """A point at the series max must not sit exactly on the top border
    (reads as clipped/pinned-to-edge) -- the y-domain gets headroom."""
    svg = sparkline_svg([0.0, 100.0, 100.0], height=180)
    root = _parse(svg)
    ys = [float(p.split(",")[1]) for p in root.find("polyline").get("points").split()]
    assert min(ys) > 4.0  # > _PADDING: real headroom above the highest point


def test_many_points_all_plotted() -> None:
    values = [float(i) for i in range(50)]
    svg = sparkline_svg(values)
    root = _parse(svg)
    polyline = root.find("polyline")
    assert len(polyline.get("points").split()) == 50


def test_custom_dimensions_and_stroke_are_honored() -> None:
    svg = sparkline_svg([1.0, 3.0, 2.0], width=300, height=80, stroke="#b02a2a")
    assert 'width="300"' in svg
    assert 'height="80"' in svg
    assert 'stroke="#b02a2a"' in svg


# --- interactive_line_chart (hover) -------------------------------------


def test_interactive_empty_renders_placeholder() -> None:
    svg = interactive_line_chart([])
    assert "chart-empty" in svg
    _parse(svg)


def test_interactive_embeds_points_for_hover() -> None:
    svg = interactive_line_chart([1.0, 2.0, 3.0], ["Jan", "Feb", "Mar"])
    root = _parse(svg)
    assert "chart-interactive" in svg
    # the hover script reads data-points; must be valid escaped JSON of len 3
    import html
    import json

    data = json.loads(html.unescape(root.get("data-points")))
    assert len(data) == 3
    assert data[0]["v"] == 1.0
    assert data[0]["l"] == "Jan"
    # crosshair + dot + hit-target scaffolding present
    assert root.find("polyline") is not None
    assert "chart-crosshair" in svg
    assert "chart-dot" in svg


def test_interactive_prefix_suffix_recorded() -> None:
    svg = interactive_line_chart([1.0, 2.0], value_prefix="$", value_suffix="%")
    root = _parse(svg)
    assert root.get("data-prefix") == "$"
    assert root.get("data-suffix") == "%"


def test_interactive_draws_three_y_gridlines_with_labels() -> None:
    svg = interactive_line_chart([100.0, 150.0, 90.0, 200.0], value_prefix="$")
    root = _parse(svg)
    lines = [el for el in root.findall("line") if el.get("stroke-dasharray") == "2 3"]
    texts = [el for el in root.findall("text") if el.text and el.text.startswith("$")]
    assert len(lines) == 3
    assert len(texts) == 3


def test_interactive_visible_line_is_a_smoothed_path() -> None:
    svg = interactive_line_chart([1.0, 5.0, 2.0, 8.0, 3.0])
    root = _parse(svg)
    path = root.find("path")
    assert path is not None
    assert " Q " in path.get("d")


def test_interactive_polyline_still_carries_every_vertex_for_hover_script() -> None:
    """base.html's hover script does svg.querySelector('polyline').points to
    position the crosshair -- the smoothed <path> above must not replace it,
    only sit visually on top of it."""
    values = [1.0, 5.0, 2.0, 8.0, 3.0]
    svg = interactive_line_chart(values)
    root = _parse(svg)
    polyline = root.find("polyline")
    assert polyline is not None
    assert len(polyline.get("points").split()) == len(values)
    assert polyline.get("opacity") == "0"


def test_interactive_area_fill_toggle() -> None:
    with_fill = interactive_line_chart([1.0, 2.0, 3.0], fill=True)
    assert "fill-opacity=\"0.08\"" in with_fill
    without = interactive_line_chart([1.0, 2.0, 3.0], fill=False)
    assert "fill-opacity=\"0.08\"" not in without


# --- scatter_svg (Story 4.9) --------------------------------------------


def test_scatter_empty_renders_placeholder_svg() -> None:
    svg = scatter_svg([])
    root = _parse(svg)
    assert root.tag.endswith("svg")
    assert "chart-empty" in svg


def test_scatter_plots_one_circle_per_point() -> None:
    svg = scatter_svg([(0.1, 5.0), (0.9, -3.0), (0.5, 0.0)])
    root = _parse(svg)
    circles = root.findall("circle")
    assert len(circles) == 3


def test_scatter_colors_wins_and_losses_differently() -> None:
    svg = scatter_svg([(0.8, 10.0), (0.8, -10.0)], win_color="#1a7a34", loss_color="#b02a2a")
    assert 'fill="#1a7a34"' in svg
    assert 'fill="#b02a2a"' in svg


def test_scatter_flat_axis_does_not_divide_by_zero() -> None:
    svg = scatter_svg([(0.5, 1.0), (0.5, 1.0)])
    root = _parse(svg)
    assert len(root.findall("circle")) == 2


def test_scatter_draws_zero_line() -> None:
    svg = scatter_svg([(0.2, 5.0), (0.8, 10.0)])
    root = _parse(svg)
    assert root.find("line") is not None
