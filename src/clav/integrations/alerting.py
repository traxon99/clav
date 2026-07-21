"""``AlertChannel`` implementations (Story 4.3): SMTP email and a generic
webhook POST (ntfy/Telegram-shaped services all accept a plain JSON/text
POST). Both are opt-in — absent config disables them at the composition
root, never here — and neither retries: a failed send is caught by
``Alerter`` and logged; the next occurrence (or the next digest) tries again,
so a single dropped alert never blocks the trading loop.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

import httpx

from clav.domain.models import Alert
from clav.interfaces.alerting import AlertChannel


class SmtpAlertChannel(AlertChannel):
    def __init__(
        self,
        *,
        host: str,
        port: int,
        use_tls: bool,
        username: str | None,
        password: str | None,
        from_addr: str,
        to_addr: str,
        timeout: float = 10.0,
    ) -> None:
        self._host = host
        self._port = port
        self._use_tls = use_tls
        self._username = username
        self._password = password
        self._from_addr = from_addr
        self._to_addr = to_addr
        self._timeout = timeout

    def send(self, alert: Alert) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[CLAV] {alert.severity.upper()}: {alert.condition}"
        msg["From"] = self._from_addr
        msg["To"] = self._to_addr
        msg.set_content(alert.message)

        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as client:
            if self._use_tls:
                client.starttls()
            if self._username is not None:
                client.login(self._username, self._password or "")
            client.send_message(msg)


class WebhookAlertChannel(AlertChannel):
    def __init__(self, *, url: str, token: str | None = None, timeout: float = 10.0) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout

    def send(self, alert: Alert) -> None:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        payload = {
            "condition": alert.condition,
            "severity": alert.severity,
            "message": alert.message,
            "context": alert.context,
        }
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(self._url, json=payload, headers=headers)
            resp.raise_for_status()
