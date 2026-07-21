"""GET /health, GET /metrics (Story 3.8, extended by Story 4.1/4.2's
``HealthMonitor``): liveness + the full ``health_snapshot`` as JSON, and the
same data as a Prometheus scrape target. Both are strictly read-only — they
render the ``health_snapshot``/``health_event`` rows ``HealthMonitor`` already
wrote inside ``clav-core``; ``clav-web`` computes nothing itself and holds no
broker keys."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from clav.clock import Clock
from clav.config import Settings
from clav.data import tables
from clav.data.repositories import Repositories
from clav.web.deps import get_clock, get_repos
from clav.web.prometheus import render_gauge

router = APIRouter(tags=["health"])

_STATUS_VALUE = {"ok": 0, "warn": 1, "critical": 2}

# A coarse "how many scheduled cycles have we missed" signal, derived from the
# existing scan cadence rather than a new config knob: missing this many is
# "degraded", missing twice that is "down" (core is stuck/dead).
_DEGRADED_CYCLE_MULTIPLE = 2
_DOWN_CYCLE_MULTIPLE = 4


def get_settings(request: Request) -> Settings:
    cfg: Settings = request.app.state.cfg
    return cfg


def _load_health_snapshot(repos: Repositories) -> dict[str, Any] | None:
    raw = repos.system_control.get("health_snapshot")
    return json.loads(raw) if raw else None


def _liveness_age_seconds(last_cycle: tables.ScanCycle | None, now: datetime) -> float | None:
    """Seconds since the last cycle *completed* — ``None`` when nothing has
    run yet, or the latest cycle is still running/failed without finishing
    (an indeterminate state we don't want to misreport as "down"; the
    liveness health_event, once HealthMonitor exists, is a stricter signal
    the dashboard can layer on top of this)."""
    if last_cycle is None or last_cycle.status != "completed" or last_cycle.finished_at is None:
        return None
    finished = last_cycle.finished_at
    finished = finished if finished.tzinfo is not None else finished.replace(tzinfo=now.tzinfo)
    return (now - finished).total_seconds()


def _has_critical(snapshot: dict[str, Any]) -> bool:
    return any(
        entry.get("status") == "critical"
        for category in snapshot.get("categories", {}).values()
        for entry in category.values()
    )


def _overall_status(
    snapshot: dict[str, Any] | None, age_seconds: float | None, scan_interval_minutes: int
) -> str:
    if age_seconds is None:
        # Nothing has completed yet (fresh install) or the state is
        # indeterminate — not evidence of a problem, so don't alarm.
        return "ok"
    if age_seconds > _DOWN_CYCLE_MULTIPLE * scan_interval_minutes * 60:
        return "down"
    if age_seconds > _DEGRADED_CYCLE_MULTIPLE * scan_interval_minutes * 60:
        return "degraded"
    if snapshot is not None and _has_critical(snapshot):
        return "degraded"
    return "ok"


@router.get("/health")
def health(
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
    cfg: Settings = Depends(get_settings),
) -> dict[str, Any]:
    estop = repos.system_control.get("emergency_stop", "false") == "true"
    paused = repos.system_control.get("paused", "false") == "true"
    last_cycle = repos.scan_cycles.latest()

    raw_budget = repos.system_control.get("llm_budget_snapshot")
    llm_budget = json.loads(raw_budget) if raw_budget else None

    snapshot = _load_health_snapshot(repos)
    now = clock.now()
    age_seconds = _liveness_age_seconds(last_cycle, now)
    status = _overall_status(snapshot, age_seconds, cfg.scan_interval_minutes)

    return {
        "status": status,
        "emergency_stop": estop,
        "paused": paused,
        "last_cycle": (
            {
                "id": last_cycle.id,
                "started_at": last_cycle.started_at.isoformat(),
                "finished_at": (
                    last_cycle.finished_at.isoformat() if last_cycle.finished_at else None
                ),
                "status": last_cycle.status,
                "mode": last_cycle.mode,
                "market_open": last_cycle.market_open,
            }
            if last_cycle is not None
            else None
        ),
        "llm_budget": llm_budget,
        "liveness": {
            "last_successful_cycle_age_seconds": age_seconds,
        },
        "categories": snapshot["categories"] if snapshot is not None else None,
        "snapshot_ts": snapshot["ts"] if snapshot is not None else None,
    }


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(
    repos: Repositories = Depends(get_repos),
    clock: Clock = Depends(get_clock),
) -> str:
    """Prometheus text-exposition format, generically derived from the same
    ``health_snapshot`` ``/health`` renders — no external client library, no
    bundled TSDB (epic decision #2): scraping/retention is the operator's
    optional, off-box choice."""
    estop = repos.system_control.get("emergency_stop", "false") == "true"
    paused = repos.system_control.get("paused", "false") == "true"
    snapshot = _load_health_snapshot(repos)
    last_cycle = repos.scan_cycles.latest()
    age_seconds = _liveness_age_seconds(last_cycle, clock.now())

    blocks = [
        render_gauge(
            "clav_last_cycle_age_seconds",
            "Seconds since the last successful scan cycle finished.",
            [({}, age_seconds)] if age_seconds is not None else [],
        ),
        render_gauge(
            "clav_emergency_stop", "1 if emergency_stop is tripped.", [({}, float(estop))]
        ),
        render_gauge("clav_paused", "1 if the trading loop is paused.", [({}, float(paused))]),
    ]

    if snapshot is not None:
        status_samples: list[tuple[dict[str, str], float]] = []
        value_samples: list[tuple[dict[str, str], float]] = []
        for category, entries in snapshot.get("categories", {}).items():
            for name, entry in entries.items():
                labels = {"category": category, "name": name}
                status_samples.append((labels, float(_STATUS_VALUE.get(entry["status"], 1))))
                for metric_name, metric_value in entry.get("value", {}).items():
                    if isinstance(metric_value, bool) or not isinstance(metric_value, int | float):
                        continue
                    value_samples.append(({**labels, "metric": metric_name}, float(metric_value)))
        blocks.append(
            render_gauge(
                "clav_health_status",
                "Health event status per category/name (0=ok, 1=warn, 2=critical).",
                status_samples,
            )
        )
        blocks.append(
            render_gauge(
                "clav_health_value",
                "Raw numeric fields from each health_event.value, per category/name/metric.",
                value_samples,
            )
        )

    return "\n".join(blocks) + "\n"
