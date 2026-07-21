"""HealthMonitor — turns each cycle's state into durable ``health_event`` rows
(Story 4.1, docs/10-observability.md §2). Every other Epic-4 story (rich
``/health``, ``/metrics``, alerting, the dashboard's health tiles) reads the
rows this writes instead of re-deriving state itself.

Five independent collectors — freshness, external, system, trading, liveness —
each wrapped so **one collector's failure never aborts the others or the
cycle**: an exception is caught, logged, and turned into a single ``warn``
``health_event`` for that collector. Time comes from the injected ``Clock``;
host-resource sampling comes from an injected ``SystemMetricsCollector``
(``clav.interfaces.system_metrics``), so tests never touch the real clock or
the real host (docs/12-roadmap.md discipline, ``tests/unit/test_architecture.py``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from clav.clock import Clock
from clav.common.logging import get_logger
from clav.config import ObservabilityConfig
from clav.data.repositories import Repositories
from clav.domain.models import AlertSeverity, HealthEvent, HealthStatus, PortfolioSnapshot
from clav.interfaces.system_metrics import SystemMetricsCollector
from clav.services.alerting import Alerter

_logger = get_logger(__name__)

_BYTES_PER_MB = 1024 * 1024


def _threshold_status(
    value: float, *, warn_at: float, critical_at: float, higher_is_worse: bool
) -> HealthStatus:
    if higher_is_worse:
        if value >= critical_at:
            return "critical"
        if value >= warn_at:
            return "warn"
        return "ok"
    if value <= critical_at:
        return "critical"
    if value <= warn_at:
        return "warn"
    return "ok"


class HealthMonitor:
    def __init__(
        self,
        *,
        clock: Clock,
        system_metrics: SystemMetricsCollector,
        db_path: Path,
        thresholds: ObservabilityConfig,
        alerter: Alerter | None = None,
    ) -> None:
        self._clock = clock
        self._system_metrics = system_metrics
        self._db_path = db_path
        self._thresholds = thresholds
        self._alerter = alerter

    def run_cycle_end(
        self,
        repos: Repositories,
        *,
        cycle_id: str,
        watchlist: list[str],
        alpaca_ok: bool,
        llm_budget_snapshot: dict[str, Any] | None,
        portfolio_snapshot: PortfolioSnapshot,
        daily_start_equity: float | None,
        max_daily_loss_pct: float,
        max_drawdown_pct: float,
        emergency_stop: bool = False,
        market_open: bool = False,
    ) -> list[HealthEvent]:
        """Run every collector, persist their events + a bounded retention
        sweep + a compact ``health_snapshot`` — never raises."""
        now = self._clock.now()
        # Read before this cycle's own liveness row is added, so the Story-4.3
        # cycle-gap check compares against the *previous* successful cycle.
        previous_liveness = repos.health_events.latest_by_name("liveness", "last_successful_cycle")
        previous_liveness_ts = previous_liveness.ts if previous_liveness is not None else None

        events: list[HealthEvent] = []
        events += self._run_collector(
            "freshness", cycle_id, self._collect_freshness, repos, watchlist, cycle_id, now
        )
        events += self._run_collector(
            "external",
            cycle_id,
            self._collect_external,
            alpaca_ok,
            llm_budget_snapshot,
            cycle_id,
            now,
        )
        events += self._run_collector("system", cycle_id, self._collect_system, cycle_id, now)
        events += self._run_collector(
            "trading",
            cycle_id,
            self._collect_trading,
            repos,
            cycle_id,
            portfolio_snapshot,
            daily_start_equity,
            max_daily_loss_pct,
            max_drawdown_pct,
            now,
        )
        events += self._run_collector("liveness", cycle_id, self._collect_liveness, cycle_id, now)
        events += self._run_collector(
            "alert",
            cycle_id,
            self._evaluate_alerts,
            repos,
            events,
            cycle_id,
            now,
            emergency_stop,
            market_open,
            previous_liveness_ts,
        )

        repos.health_events.add_many(events)
        repos.health_events.prune(keep_per_category=self._thresholds.retention_per_category)
        self._persist_snapshot(repos, events, now)
        if self._alerter is not None:
            self._alerter.tick()
        return events

    def record_startup(self, repos: Repositories) -> list[HealthEvent]:
        """Lightweight startup check (Story 4.1): system resources +
        a "process started" liveness marker, before the first cycle has run."""
        now = self._clock.now()
        events = self._run_collector("system", None, self._collect_system, None, now)
        events.append(
            HealthEvent(ts=now, category="liveness", name="process_started", status="ok", value={})
        )
        repos.health_events.add_many(events)
        return events

    def _run_collector(
        self,
        category: str,
        cycle_id: str | None,
        collector: Callable[..., list[HealthEvent]],
        *args: Any,
    ) -> list[HealthEvent]:
        try:
            return collector(*args)
        except Exception as exc:
            _logger.warning("health_monitor_collector_failed", category=category, error=str(exc))
            return [
                HealthEvent(
                    ts=self._clock.now(),
                    category=category,
                    name="collector_error",
                    status="warn",
                    value={"error": str(exc)},
                    cycle_id=cycle_id,
                )
            ]

    # --- collectors --------------------------------------------------------

    def _collect_freshness(
        self, repos: Repositories, watchlist: list[str], cycle_id: str, now: Any
    ) -> list[HealthEvent]:
        warn_h = self._thresholds.freshness_warn_hours
        critical_h = self._thresholds.freshness_critical_hours
        events: list[HealthEvent] = []
        for symbol in watchlist:
            instrument = repos.instruments.get_by_symbol(symbol)
            if instrument is None:
                continue
            checks: list[tuple[str, Any]] = [
                ("candle", repos.candles.latest_ts(instrument.id, "1Day")),
                ("indicator", repos.indicator_sets.latest_ts(instrument.id)),
                ("news", repos.news_items.latest_ts(instrument.id)),
            ]
            social = repos.social_digests.latest(instrument.id)
            checks.append(("social", social.generated_at if social is not None else None))

            for kind, ts in checks:
                if ts is None:
                    status: HealthStatus = "warn"
                    age_hours = None
                else:
                    ts = ts if ts.tzinfo is not None else ts.replace(tzinfo=now.tzinfo)
                    age_hours = (now - ts).total_seconds() / 3600.0
                    status = _threshold_status(
                        age_hours, warn_at=warn_h, critical_at=critical_h, higher_is_worse=True
                    )
                events.append(
                    HealthEvent(
                        ts=now,
                        category="freshness",
                        name=f"{symbol}:{kind}",
                        status=status,
                        value={"age_hours": age_hours},
                        cycle_id=cycle_id,
                    )
                )
        return events

    def _collect_external(
        self,
        alpaca_ok: bool,
        llm_budget_snapshot: dict[str, Any] | None,
        cycle_id: str,
        now: Any,
    ) -> list[HealthEvent]:
        events = [
            HealthEvent(
                ts=now,
                category="external",
                name="alpaca",
                status="ok" if alpaca_ok else "critical",
                value={"ok": alpaca_ok},
                cycle_id=cycle_id,
            )
        ]
        if llm_budget_snapshot is not None:
            status: HealthStatus = "ok"
            if llm_budget_snapshot.get("breaker_open"):
                status = "critical"
            elif llm_budget_snapshot.get("budget_exhausted"):
                status = "warn"
            events.append(
                HealthEvent(
                    ts=now,
                    category="external",
                    name="gemini",
                    status=status,
                    value=llm_budget_snapshot,
                    cycle_id=cycle_id,
                )
            )
        return events

    def _collect_system(self, cycle_id: str | None, now: Any) -> list[HealthEvent]:
        t = self._thresholds
        free_mem_mb = self._system_metrics.free_memory_bytes() / _BYTES_PER_MB
        disk_free_mb = self._system_metrics.disk_free_bytes(self._db_path.parent) / _BYTES_PER_MB
        cpu_pct = self._system_metrics.cpu_load_pct()
        rss_mb = self._system_metrics.process_rss_bytes() / _BYTES_PER_MB
        db_size_mb, wal_size_mb = self._db_sizes_mb()

        return [
            HealthEvent(
                ts=now,
                category="system",
                name="process_rss",
                status="ok",
                value={"rss_mb": rss_mb},
                cycle_id=cycle_id,
            ),
            HealthEvent(
                ts=now,
                category="system",
                name="free_memory",
                status=_threshold_status(
                    free_mem_mb,
                    warn_at=t.free_memory_warn_mb,
                    critical_at=t.free_memory_critical_mb,
                    higher_is_worse=False,
                ),
                value={"free_mb": free_mem_mb},
                cycle_id=cycle_id,
            ),
            HealthEvent(
                ts=now,
                category="system",
                name="cpu_load",
                status=_threshold_status(
                    cpu_pct,
                    warn_at=t.cpu_warn_pct,
                    critical_at=t.cpu_critical_pct,
                    higher_is_worse=True,
                ),
                value={"cpu_pct": cpu_pct},
                cycle_id=cycle_id,
            ),
            HealthEvent(
                ts=now,
                category="system",
                name="disk_free",
                status=_threshold_status(
                    disk_free_mb,
                    warn_at=t.disk_free_warn_mb,
                    critical_at=t.disk_free_critical_mb,
                    higher_is_worse=False,
                ),
                value={"free_mb": disk_free_mb},
                cycle_id=cycle_id,
            ),
            HealthEvent(
                ts=now,
                category="system",
                name="db_size",
                status="ok",
                value={"db_mb": db_size_mb, "wal_mb": wal_size_mb},
                cycle_id=cycle_id,
            ),
        ]

    def _db_sizes_mb(self) -> tuple[float, float]:
        db_size = self._db_path.stat().st_size if self._db_path.exists() else 0
        wal_path = self._db_path.with_name(self._db_path.name + "-wal")
        wal_size = wal_path.stat().st_size if wal_path.exists() else 0
        return db_size / _BYTES_PER_MB, wal_size / _BYTES_PER_MB

    def _collect_trading(
        self,
        repos: Repositories,
        cycle_id: str,
        portfolio_snapshot: PortfolioSnapshot,
        daily_start_equity: float | None,
        max_daily_loss_pct: float,
        max_drawdown_pct: float,
        now: Any,
    ) -> list[HealthEvent]:
        decisions_by_action = repos.decisions.count_by_action_for_cycle(cycle_id)
        orders_by_status = repos.orders.count_by_status_for_cycle(cycle_id)

        drawdown_status: HealthStatus = _threshold_status(
            portfolio_snapshot.drawdown,
            warn_at=max_drawdown_pct * 0.7,
            critical_at=max_drawdown_pct,
            higher_is_worse=True,
        )

        daily_pnl_pct: float | None = None
        daily_pnl_status: HealthStatus = "ok"
        if daily_start_equity is not None and daily_start_equity > 0:
            daily_pnl_pct = (portfolio_snapshot.equity - daily_start_equity) / daily_start_equity
            loss_pct = max(0.0, -daily_pnl_pct)
            daily_pnl_status = _threshold_status(
                loss_pct,
                warn_at=max_daily_loss_pct * 0.7,
                critical_at=max_daily_loss_pct,
                higher_is_worse=True,
            )

        return [
            HealthEvent(
                ts=now,
                category="trading",
                name="decisions_by_action",
                status="ok",
                value={"counts": decisions_by_action},
                cycle_id=cycle_id,
            ),
            HealthEvent(
                ts=now,
                category="trading",
                name="orders_by_status",
                status="ok",
                value={"counts": orders_by_status},
                cycle_id=cycle_id,
            ),
            HealthEvent(
                ts=now,
                category="trading",
                name="drawdown",
                status=drawdown_status,
                value={"drawdown": portfolio_snapshot.drawdown, "cap": max_drawdown_pct},
                cycle_id=cycle_id,
            ),
            HealthEvent(
                ts=now,
                category="trading",
                name="daily_pnl_vs_cap",
                status=daily_pnl_status,
                value={"daily_pnl_pct": daily_pnl_pct, "cap": max_daily_loss_pct},
                cycle_id=cycle_id,
            ),
        ]

    def _collect_liveness(self, cycle_id: str, now: Any) -> list[HealthEvent]:
        return [
            HealthEvent(
                ts=now,
                category="liveness",
                name="last_successful_cycle",
                status="ok",
                value={"cycle_id": cycle_id, "finished_at": now.isoformat()},
                cycle_id=cycle_id,
            )
        ]

    def _evaluate_alerts(
        self,
        repos: Repositories,
        events: list[HealthEvent],
        cycle_id: str,
        now: datetime,
        emergency_stop: bool,
        market_open: bool,
        previous_liveness_ts: datetime | None,
    ) -> list[HealthEvent]:
        """Story 4.3 — the docs/10 §3 trigger conditions this monitor can see
        from what it just collected: memory/disk pressure, broker/LLM
        external state, drawdown/daily-loss breaches, an emergency-stop
        edge-trip (however it was tripped — auto breaker or the operator via
        clav-web, both just flip the same polled flag), and a retrospective
        "no successful cycle in > N minutes during market hours" gap check.
        A no-op (returns nothing, notifies nothing) when no ``Alerter`` is
        configured."""
        if self._alerter is None:
            return []

        alerts: list[HealthEvent] = []
        by_key = {(e.category, e.name): e for e in events}

        def raise_alert(
            condition: str, severity: AlertSeverity, message: str, context: dict[str, Any]
        ) -> None:
            assert self._alerter is not None
            self._alerter.notify(condition, severity, message, context)
            alerts.append(
                HealthEvent(
                    ts=now,
                    category="alert",
                    name=condition,
                    status="critical" if severity == "critical" else "warn",
                    value={"message": message, **context},
                    cycle_id=cycle_id,
                )
            )

        for name, condition in (("free_memory", "memory_pressure"), ("disk_free", "disk_pressure")):
            event = by_key.get(("system", name))
            if event is not None and event.status != "ok":
                severity: AlertSeverity = "critical" if event.status == "critical" else "warning"
                raise_alert(condition, severity, f"{name} is {event.status}", event.value)

        alpaca = by_key.get(("external", "alpaca"))
        if alpaca is not None and alpaca.status == "critical":
            raise_alert(
                "broker_unreachable",
                "critical",
                "Alpaca was unreachable this cycle",
                alpaca.value,
            )

        gemini = by_key.get(("external", "gemini"))
        if gemini is not None:
            if gemini.status == "critical":
                raise_alert(
                    "llm_breaker_open", "critical", "Gemini circuit breaker is open", gemini.value
                )
            elif gemini.value.get("budget_exhausted"):
                raise_alert(
                    "llm_budget_exhausted",
                    "warning",
                    "Gemini daily token/cost budget is exhausted",
                    gemini.value,
                )

        drawdown = by_key.get(("trading", "drawdown"))
        if drawdown is not None and drawdown.status == "critical":
            raise_alert(
                "drawdown_breach", "critical", "Portfolio drawdown breached its cap", drawdown.value
            )

        daily_pnl = by_key.get(("trading", "daily_pnl_vs_cap"))
        if daily_pnl is not None and daily_pnl.status == "critical":
            raise_alert(
                "daily_loss_cap_hit",
                "critical",
                "Daily loss approached/breached its cap",
                daily_pnl.value,
            )

        previously_tripped = (
            repos.system_control.get("alert_prev_emergency_stop", "false") == "true"
        )
        if emergency_stop and not previously_tripped:
            raise_alert("emergency_stop_tripped", "critical", "emergency_stop is now set", {})
        repos.system_control.set(
            "alert_prev_emergency_stop",
            "true" if emergency_stop else "false",
            updated_at=now,
            updated_by="system:health_monitor",
        )

        if market_open and previous_liveness_ts is not None:
            previous_liveness_ts = (
                previous_liveness_ts
                if previous_liveness_ts.tzinfo is not None
                else previous_liveness_ts.replace(tzinfo=now.tzinfo)
            )
            gap_minutes = (now - previous_liveness_ts).total_seconds() / 60.0
            if gap_minutes > self._thresholds.max_cycle_gap_minutes:
                raise_alert(
                    "cycle_gap_exceeded",
                    "critical",
                    f"{gap_minutes:.1f} min since the previous successful cycle "
                    f"(cap {self._thresholds.max_cycle_gap_minutes})",
                    {"gap_minutes": gap_minutes},
                )

        return alerts

    # --- snapshot ------------------------------------------------------------

    def _persist_snapshot(self, repos: Repositories, events: list[HealthEvent], now: Any) -> None:
        """Compact latest-state snapshot to ``system_control`` (mirrors Epic
        3's ``llm_budget_snapshot`` pattern) so the separate ``clav-web``
        process can render current health without recomputing it."""
        categories: dict[str, dict[str, Any]] = {}
        for event in events:
            categories.setdefault(event.category, {})[event.name] = {
                "status": event.status,
                "value": event.value,
            }
        snapshot = {"ts": now.isoformat(), "categories": categories}
        repos.system_control.set(
            "health_snapshot",
            json.dumps(snapshot),
            updated_at=now,
            updated_by="system:health_monitor",
        )
