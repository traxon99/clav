"""A tiny hand-rolled Prometheus text-exposition formatter (Story 4.2) —
not the ``prometheus_client`` package: CLAV's metric surface is small and
this keeps ``clav-web`` dependency-light (Pi discipline, docs/12-roadmap.md
decision #2). Scraping/retention with an off-box Prometheus is the
operator's optional choice; CLAV never bundles a TSDB.
"""

from __future__ import annotations

from collections.abc import Iterable

_LABEL_ESCAPE = str.maketrans({"\\": "\\\\", '"': '\\"', "\n": "\\n"})


def _format_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    pairs = ",".join(f'{k}="{v.translate(_LABEL_ESCAPE)}"' for k, v in labels.items())
    return "{" + pairs + "}"


def render_gauge(name: str, help_text: str, samples: Iterable[tuple[dict[str, str], float]]) -> str:
    """One HELP/TYPE block + a data line per ``(labels, value)`` sample, in
    Prometheus text-exposition format."""
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
    for labels, value in samples:
        lines.append(f"{name}{_format_labels(labels)} {value}")
    return "\n".join(lines)
