"""SystemMetricsCollector — the port for host-resource sampling (Story 4.1).

``HealthMonitor`` reads process/host resource usage through this interface
instead of calling ``psutil`` directly, so tests can stub resource pressure
(low memory/disk/CPU) deterministically without touching the real host.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class SystemMetricsCollector(ABC):
    @abstractmethod
    def process_rss_bytes(self) -> int:
        """Resident set size of the current process, in bytes."""

    @abstractmethod
    def free_memory_bytes(self) -> int:
        """System-wide available memory, in bytes."""

    @abstractmethod
    def cpu_load_pct(self) -> float:
        """Current system-wide CPU utilization, 0-100."""

    @abstractmethod
    def disk_free_bytes(self, path: Path) -> int:
        """Free space on the filesystem containing ``path``, in bytes."""
