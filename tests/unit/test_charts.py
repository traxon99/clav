"""Story 4.5 — sparkline_svg: a tiny inline-SVG line-chart helper. No JS,
no CDN; must degrade gracefully on 0/1-point series instead of dividing by
zero, and produce valid, parseable SVG for a real series."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from clav.web.charts import scatter_svg, sparkline_svg


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
