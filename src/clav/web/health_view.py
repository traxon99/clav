"""Shared health-snapshot reading (Story 4.1/4.2's ``health_snapshot``) so
``GET /health``, ``GET /metrics``, and the dashboard's Story-4.8 tiles never
disagree about what "healthy" means — one source of truth for status
derivation, all strictly read-only over rows ``HealthMonitor`` already wrote.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from clav.data import tables
from clav.data.repositories import Repositories

STATUS_RANK = {"ok": 0, "warn": 1, "critical": 2}

# A coarse "how many scheduled cycles have we missed" signal, derived from the
# existing scan cadence rather than a new config knob: missing this many is
# "degraded", missing twice that is "down" (core is stuck/dead).
DEGRADED_CYCLE_MULTIPLE = 2
DOWN_CYCLE_MULTIPLE = 4

UNKNOWN = "unknown"


def load_health_snapshot(repos: Repositories) -> dict[str, Any] | None:
    raw = repos.system_control.get("health_snapshot")
    return json.loads(raw) if raw else None


def liveness_age_seconds(last_cycle: tables.ScanCycle | None, now: datetime) -> float | None:
    """Seconds since the last cycle *completed* — ``None`` when nothing has
    run yet, or the latest cycle is still running/failed without finishing
    (an indeterminate state we don't want to misreport as "down")."""
    if last_cycle is None or last_cycle.status != "completed" or last_cycle.finished_at is None:
        return None
    finished = last_cycle.finished_at
    finished = finished if finished.tzinfo is not None else finished.replace(tzinfo=now.tzinfo)
    return (now - finished).total_seconds()


def has_critical(snapshot: dict[str, Any]) -> bool:
    return any(
        entry.get("status") == "critical"
        for category in snapshot.get("categories", {}).values()
        for entry in category.values()
    )


def overall_status(
    snapshot: dict[str, Any] | None, age_seconds: float | None, scan_interval_minutes: int
) -> str:
    if age_seconds is None:
        # Nothing has completed yet (fresh install) or the state is
        # indeterminate — not evidence of a problem, so don't alarm.
        return "ok"
    if age_seconds > DOWN_CYCLE_MULTIPLE * scan_interval_minutes * 60:
        return "down"
    if age_seconds > DEGRADED_CYCLE_MULTIPLE * scan_interval_minutes * 60:
        return "degraded"
    if snapshot is not None and has_critical(snapshot):
        return "degraded"
    return "ok"


def _worst_by_kind(freshness_events: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group per-symbol freshness entries (``name="SYMBOL:kind"``) by kind,
    keeping the worst status per kind — a compact per-source tile instead of
    one badge per watchlist symbol."""
    by_kind: dict[str, dict[str, Any]] = {}
    for name, entry in freshness_events.items():
        _, _, kind = name.partition(":")
        if not kind:
            continue
        current = by_kind.get(kind)
        if current is None or STATUS_RANK[entry["status"]] > STATUS_RANK[current["status"]]:
            by_kind[kind] = entry
    return by_kind


def _fmt_num(value: Any) -> str:
    return f"{value:.1f}" if isinstance(value, float) else str(value)


def _freshness_tile(kind: str, entry: dict[str, Any]) -> dict[str, str]:
    age = entry.get("value", {}).get("age_hours")
    display = f"{age:.1f}h old" if age is not None else UNKNOWN
    return {"name": f"freshness: {kind}", "status": entry["status"], "display": display}


def _system_tile(name: str, entry: dict[str, Any]) -> dict[str, str]:
    parts = [f"{k}={_fmt_num(v)}" for k, v in entry.get("value", {}).items()]
    return {"name": name, "status": entry["status"], "display": ", ".join(parts) or UNKNOWN}


def _alpaca_tile(entry: dict[str, Any] | None) -> dict[str, str]:
    status = entry["status"] if entry is not None else UNKNOWN
    return {"name": "alpaca", "status": status, "display": status}


def _gemini_tile(entry: dict[str, Any] | None) -> dict[str, str]:
    if entry is None:
        return {"name": "gemini", "status": UNKNOWN, "display": UNKNOWN}
    value = entry.get("value", {})
    tokens = value.get("daily_tokens")
    budget = value.get("daily_token_budget")
    display = f"{tokens}/{budget} tokens" if tokens is not None and budget is not None else UNKNOWN
    if value.get("breaker_open"):
        display += " (breaker open)"
    return {"name": "gemini", "status": entry["status"], "display": display}


def _daily_pnl_tile(entry: dict[str, Any] | None) -> dict[str, str]:
    if entry is None:
        return {"name": "daily P&L vs cap", "status": UNKNOWN, "display": UNKNOWN}
    value = entry.get("value", {})
    pct, cap = value.get("daily_pnl_pct"), value.get("cap")
    display = (
        f"{pct * 100:.2f}% (cap {cap * 100:.2f}%)"
        if pct is not None and cap is not None
        else UNKNOWN
    )
    return {"name": "daily P&L vs cap", "status": entry["status"], "display": display}


def build_health_view(
    repos: Repositories, now: datetime, *, scan_interval_minutes: int
) -> dict[str, Any]:
    """Story 4.8 — the dashboard's at-a-glance health-tile data: liveness,
    freshness-by-source, breaker/budget badges, system tiles, and the
    daily-loss gauge, all flattened to a uniform ``tiles`` list so the
    template just iterates — no per-field Jinja formatting. Every tile
    degrades to ``"unknown"`` rather than raising when a metric hasn't been
    collected yet (a fresh install, or one predating Epic 4)."""
    snapshot = load_health_snapshot(repos)
    last_cycle = repos.scan_cycles.latest()
    age_seconds = liveness_age_seconds(last_cycle, now)
    status = overall_status(snapshot, age_seconds, scan_interval_minutes)
    categories: dict[str, dict[str, Any]] = snapshot["categories"] if snapshot is not None else {}

    tiles: list[dict[str, str]] = [
        {
            "name": "liveness",
            "status": status,
            "display": f"{age_seconds:.0f}s since last cycle"
            if age_seconds is not None
            else UNKNOWN,
        },
        _alpaca_tile(categories.get("external", {}).get("alpaca")),
        _gemini_tile(categories.get("external", {}).get("gemini")),
    ]

    freshness_by_kind = _worst_by_kind(categories.get("freshness", {}))
    if freshness_by_kind:
        tiles += [_freshness_tile(kind, entry) for kind, entry in sorted(freshness_by_kind.items())]
    else:
        tiles.append({"name": "freshness", "status": UNKNOWN, "display": UNKNOWN})

    system = categories.get("system", {})
    if system:
        tiles += [_system_tile(name, entry) for name, entry in sorted(system.items())]
    else:
        tiles.append({"name": "system", "status": UNKNOWN, "display": UNKNOWN})

    tiles.append(_daily_pnl_tile(categories.get("trading", {}).get("daily_pnl_vs_cap")))

    return {"status": status, "tiles": tiles, "has_snapshot": snapshot is not None}
