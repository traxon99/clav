"""Story 4.3 — SmtpAlertChannel / WebhookAlertChannel: no live network in CI
(matching the news/social adapters' convention), so ``smtplib``/``httpx``
are mocked at the module boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from clav.domain.models import Alert
from clav.integrations.alerting import SmtpAlertChannel, WebhookAlertChannel

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _alert(**overrides: object) -> Alert:
    defaults: dict[str, object] = {
        "condition": "broker_down",
        "severity": "critical",
        "message": "Alpaca unreachable",
        "context": {},
    }
    defaults.update(overrides)
    return Alert(**defaults)  # type: ignore[arg-type]


def test_smtp_channel_sends_with_tls_and_login() -> None:
    with patch("clav.integrations.alerting.smtplib.SMTP") as smtp_cls:
        client = smtp_cls.return_value.__enter__.return_value
        channel = SmtpAlertChannel(
            host="smtp.example.com",
            port=587,
            use_tls=True,
            username="user",
            password="pw",
            from_addr="clav@example.com",
            to_addr="you@example.com",
        )
        channel.send(_alert())

    smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=10.0)
    client.starttls.assert_called_once()
    client.login.assert_called_once_with("user", "pw")
    client.send_message.assert_called_once()
    sent_msg = client.send_message.call_args[0][0]
    assert sent_msg["From"] == "clav@example.com"
    assert sent_msg["To"] == "you@example.com"
    assert "CRITICAL" in sent_msg["Subject"]
    assert "broker_down" in sent_msg["Subject"]


def test_smtp_channel_skips_tls_and_login_when_not_configured() -> None:
    with patch("clav.integrations.alerting.smtplib.SMTP") as smtp_cls:
        client = smtp_cls.return_value.__enter__.return_value
        channel = SmtpAlertChannel(
            host="localhost",
            port=25,
            use_tls=False,
            username=None,
            password=None,
            from_addr="clav@example.com",
            to_addr="you@example.com",
        )
        channel.send(_alert())

    client.starttls.assert_not_called()
    client.login.assert_not_called()
    client.send_message.assert_called_once()


def test_smtp_channel_propagates_send_failure() -> None:
    """The channel itself doesn't swallow errors — Alerter._dispatch does."""
    with patch("clav.integrations.alerting.smtplib.SMTP") as smtp_cls:
        client = smtp_cls.return_value.__enter__.return_value
        client.send_message.side_effect = OSError("connection refused")
        channel = SmtpAlertChannel(
            host="smtp.example.com",
            port=587,
            use_tls=True,
            username=None,
            password=None,
            from_addr="a",
            to_addr="b",
        )
        try:
            channel.send(_alert())
        except OSError:
            pass
        else:
            raise AssertionError("expected the send failure to propagate")


def test_webhook_channel_posts_json_payload() -> None:
    with patch("clav.integrations.alerting.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = MagicMock(raise_for_status=MagicMock())
        channel = WebhookAlertChannel(url="https://ntfy.sh/topic", token="secret-token")
        channel.send(_alert(context={"free_mb": 50.0}))

    client.post.assert_called_once()
    args, kwargs = client.post.call_args
    assert args[0] == "https://ntfy.sh/topic"
    assert kwargs["json"] == {
        "condition": "broker_down",
        "severity": "critical",
        "message": "Alpaca unreachable",
        "context": {"free_mb": 50.0},
    }
    assert kwargs["headers"] == {"Authorization": "Bearer secret-token"}


def test_webhook_channel_omits_auth_header_without_a_token() -> None:
    with patch("clav.integrations.alerting.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = MagicMock(raise_for_status=MagicMock())
        channel = WebhookAlertChannel(url="https://ntfy.sh/topic")
        channel.send(_alert())

    _, kwargs = client.post.call_args
    assert kwargs["headers"] == {}


def test_webhook_channel_raises_on_http_error() -> None:
    with patch("clav.integrations.alerting.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        response = MagicMock()
        response.raise_for_status.side_effect = RuntimeError("500 server error")
        client.post.return_value = response
        channel = WebhookAlertChannel(url="https://ntfy.sh/topic")

        try:
            channel.send(_alert())
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the HTTP error to propagate")
