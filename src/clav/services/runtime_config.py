"""RuntimeConfigStore — operator-tunable weights/risk-knobs/watchlist overrides
(Story 3.8), persisted in the Epic-1 ``system_control`` key/value store so a
change made through the control API takes effect on the **very next scan
cycle** — ``ScanCycleService`` re-reads and merges the override each cycle, no
``clav-core`` restart required.

Only a deliberately small, named slice of config is overridable this way
(weights, thresholds, a handful of risk knobs, watchlist); everything else
(Alpaca keys, mode, trading window, earnings calendar, ...) stays fixed at
boot. Every write is re-validated with the exact same Pydantic constraints as
boot-time config (``RuntimeOverrides``/``WeightsConfig``/``ThresholdsConfig``/
``RiskKnobsOverride``), so a write can never relax a value past what
``config.yaml`` itself would reject. ``scan_interval_minutes`` is persisted and
validated here but is **not** live-rescheduled in this story — the running
APScheduler cadence still comes from boot config (documented limitation, see
the Story 3.12 runbook).

This store deliberately holds no boot-time defaults itself (unset fields mean
"no override") — ``ScanCycleService`` merges the override on top of its own
boot-time values each cycle, so this class stays a thin, easily-testable
read/write/validate layer over ``system_control``.
"""

from __future__ import annotations

from datetime import datetime

from clav.config import RuntimeOverrides
from clav.data.repositories import Repositories

_SYSTEM_CONTROL_KEY = "runtime_config_override"


class RuntimeConfigStore:
    def get(self, repos: Repositories) -> RuntimeOverrides:
        raw = repos.system_control.get(_SYSTEM_CONTROL_KEY)
        if raw is None:
            return RuntimeOverrides()
        return RuntimeOverrides.model_validate_json(raw)

    def set(
        self, repos: Repositories, overrides: RuntimeOverrides, *, now: datetime, updated_by: str
    ) -> None:
        repos.system_control.set(
            _SYSTEM_CONTROL_KEY,
            overrides.model_dump_json(),
            updated_at=now,
            updated_by=updated_by,
        )
