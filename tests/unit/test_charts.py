"""Story 4.5 — sparkline_svg: a tiny inline-SVG line-chart helper. No JS,
no CDN; must degrade gracefully on 0/1-point series instead of dividing by
zero, and produce valid, parseable SVG for a real series."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from clav.web.charts import sparkline_svg


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
