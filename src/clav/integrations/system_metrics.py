"""``psutil``-backed ``SystemMetricsCollector`` (Story 4.1).

``psutil.cpu_percent(interval=None)`` is intentionally non-blocking: it
compares against the last call rather than sleeping, which is what keeps
``HealthMonitor`` from stalling a scan cycle to sample CPU.
"""

from __future__ import annotations

import os
from pathlib import Path

import psutil

from clav.interfaces.system_metrics import SystemMetricsCollector


class PsutilSystemMetricsCollector(SystemMetricsCollector):
    def process_rss_bytes(self) -> int:
        return int(psutil.Process(os.getpid()).memory_info().rss)

    def free_memory_bytes(self) -> int:
        return psutil.virtual_memory().available

    def cpu_load_pct(self) -> float:
        return psutil.cpu_percent(interval=None)

    def disk_free_bytes(self, path: Path) -> int:
        probe = path if path.exists() else path.parent
        return psutil.disk_usage(str(probe)).free
