"""GET /audit, GET /audit/cycle/{cycle_id} (Story 4.7): a searchable browser
over the durable DB journal — ``audit_log`` + ``health_event`` — filterable
by cycle_id/category/severity, newest-first, paginated, plus a one-click
"reconstruct this cycle" view joining config_snapshot -> decisions -> risk
evals -> orders -> health events -> audit log for one cycle. Read-only,
bounded queries throughout (epic decision #5): this searches the durable DB
journal, not a log-file grep — verbose structured file logs stay on disk/
journald (``journalctl -u clav-core``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from clav.data import tables
from clav.data.repositories import Repositories
from clav.domain.models import HealthEvent
from clav.web.deps import get_repos

router = APIRouter(tags=["audit"])

_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Bounds a single page regardless of the ?limit= query param.
MAX_PAGE_LIMIT = 100
DEFAULT_PAGE_LIMIT = 25

_HEALTH_STATUSES = {"ok", "warn", "critical"}


def _token(request: Request) -> str | None:
    token: str | None = request.app.state.web_token
    return token


def _health_event_row(event: HealthEvent) -> dict[str, Any]:
    return {
        "ts": event.ts,
        "kind": "health_event",
        "category": event.category,
        "severity": event.status,
        "cycle_id": event.cycle_id,
        "summary": f"{event.category}: {event.name}",
    }


def _audit_log_row(entry: tables.AuditLog) -> dict[str, Any]:
    return {
        "ts": entry.ts,
        "kind": "audit",
        "category": "audit",
        "severity": "info",
        "cycle_id": entry.correlation_id,
        "summary": f"{entry.actor} {entry.action} on {entry.entity_type}/{entry.entity_id}",
    }


@router.get("/audit", response_class=HTMLResponse)
def audit_list(
    request: Request,
    cycle_id: str = "",
    category: str = "",
    severity: str = "",
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    repos: Repositories = Depends(get_repos),
) -> HTMLResponse:
    bounded_limit = max(1, min(limit, MAX_PAGE_LIMIT))
    bounded_offset = max(0, offset)
    cid = cycle_id.strip() or None
    cat = category.strip() or None
    sev = severity.strip() or None

    # A category/severity outside "audit"/"info" only ever matches
    # health_event rows, and vice versa -- so each source is skipped
    # entirely rather than run with a filter it can never satisfy.
    show_audit = cat in (None, "audit") and sev in (None, "info")
    show_health = cat != "audit" and sev != "info"
    health_category = cat if cat not in (None, "audit") else None
    health_status = sev if sev in _HEALTH_STATUSES else None

    rows: list[dict[str, Any]] = []
    total = 0
    if show_health:
        rows += [
            _health_event_row(e)
            for e in repos.health_events.list_recent(
                category=health_category,
                status=health_status,
                cycle_id=cid,
                limit=bounded_limit,
                offset=bounded_offset,
            )
        ]
        total += repos.health_events.count_recent(
            category=health_category, status=health_status, cycle_id=cid
        )
    if show_audit:
        rows += [
            _audit_log_row(a)
            for a in repos.audit_log.list_recent(
                correlation_id=cid, limit=bounded_limit, offset=bounded_offset
            )
        ]
        total += repos.audit_log.count_recent(correlation_id=cid)

    rows.sort(key=lambda r: r["ts"], reverse=True)
    rows = rows[:bounded_limit]

    return _templates.TemplateResponse(
        request,
        "audit.html",
        {
            "rows": rows,
            "cycle_id": cycle_id,
            "category": category,
            "severity": severity,
            "limit": bounded_limit,
            "offset": bounded_offset,
            "has_more": bounded_offset + bounded_limit < total,
            "token": _token(request),
        },
    )


@router.get("/audit/cycle/{cycle_id}", response_class=HTMLResponse)
def reconstruct_cycle(
    request: Request, cycle_id: str, repos: Repositories = Depends(get_repos)
) -> HTMLResponse:
    scan_cycle = repos.scan_cycles.get(cycle_id)
    config_snapshot = repos.config_snapshots.get_by_cycle_id(cycle_id)

    decisions: list[dict[str, Any]] = []
    for decision in repos.decisions.list_by_cycle(cycle_id):
        instrument = repos.instruments.get_by_id(decision.instrument_id)
        llm = (decision.reasoning or {}).get("llm")
        decisions.append(
            {
                "id": decision.id,
                "symbol": instrument.symbol if instrument is not None else "",
                "action": decision.action,
                "is_llm_driven": llm is not None,
                "risk_evaluation": repos.risk_evaluations.get_by_decision_id(decision.id),
                "order": repos.orders.get_by_decision_id(decision.id),
            }
        )

    # Bounded: a cycle's own health_event/audit_log rows are inherently a
    # small, fixed-size set (one cycle's worth), never the whole table.
    health_events = repos.health_events.list_recent(cycle_id=cycle_id, limit=200)
    audit_entries = repos.audit_log.list_recent(correlation_id=cycle_id, limit=200)

    return _templates.TemplateResponse(
        request,
        "audit_cycle.html",
        {
            "cycle_id": cycle_id,
            "scan_cycle": scan_cycle,
            "config_snapshot": config_snapshot,
            "config_snapshot_json": (
                json.dumps(config_snapshot.config, indent=2, default=str)
                if config_snapshot is not None
                else None
            ),
            "decisions": decisions,
            "health_events": health_events,
            "audit_entries": audit_entries,
            "token": _token(request),
        },
    )
