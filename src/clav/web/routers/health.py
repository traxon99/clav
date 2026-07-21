"""GET /health, GET /metrics (Story 3.8, extended by Story 4.1/4.2's
``HealthMonitor``): liveness + the full ``health_snapshot`` as JSON, and the
same data as a Prometheus scrape target. Both are strictly read-only — they
render the ``health_snapshot``/``health_event`` rows ``HealthMonitor`` already
wrote inside ``clav-core``; ``clav-web`` computes nothing itself and holds no
broker keys. Status derivation is shared with the Story-4.8 dashboard tiles
via ``clav.web.health_view`` so the two surfaces never disagree.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from clav.clock import Clock
from clav.config import Settings
from clav.data.repositories import Repositories
from clav.web.deps import get_clock, get_repos
from clav.web.health_view import (
    STATUS_RANK,
    liveness_age_seconds,
    load_health_snapshot,
    overall_status,
)
from clav.web.prometheus import render_gauge

router = APIRouter(tags=["health"])


def get_settings(request: Request) -> Settings:
    cfg: Settings = request.app.state.cfg
    return cfg


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

    snapshot = load_health_snapshot(repos)
    now = clock.now()
    age_seconds = liveness_age_seconds(last_cycle, now)
    status = overall_status(snapshot, age_seconds, cfg.scan_interval_minutes)

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
    snapshot = load_health_snapshot(repos)
    last_cycle = repos.scan_cycles.latest()
    age_seconds = liveness_age_seconds(last_cycle, clock.now())

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
                status_samples.append((labels, float(STATUS_RANK.get(entry["status"], 1))))
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
