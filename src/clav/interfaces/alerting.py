"""AlertChannel — the port for paging a human (Story 4.3).

Implementations (SMTP email, webhook) are best-effort: a channel's ``send``
may raise on a transient failure, but the caller (``Alerter``) always catches
it — a broken/unconfigured channel must never crash the cycle or silently
swallow the fact that an alert happened (that's why every alert is *also* a
``health_event`` row, written independently of whether any channel exists).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from clav.domain.models import Alert, AlertSeverity

__all__ = ["Alert", "AlertChannel", "AlertSeverity"]


class AlertChannel(ABC):
    @abstractmethod
    def send(self, alert: Alert) -> None:
        """Deliver ``alert``. May raise — the caller is responsible for
        catching and logging; a channel must never be trusted to fail safe
        on its own."""
