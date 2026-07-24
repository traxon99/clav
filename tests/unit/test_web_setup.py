"""The /setup page: an operator can paste Alpaca paper keys into the
dashboard instead of hand-editing .env. Values are write-only -- never
rendered back -- and land in a per-app-configured .env path, never the
developer's real repo-root .env."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from clav.clock import FakeClock
from clav.config import Settings
from clav.data.db import make_engine
from clav.data.tables import Base
from clav.web.main import create_app

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _settings(tmp_path, *, token: str | None = None) -> Settings:
    overrides: dict = {
        "watchlist": ["AAPL"],
        "alpaca": {"api_key": "k", "api_secret": "s"},
        "data_dir": tmp_path,
    }
    if token is not None:
        overrides["web"] = {"token": token}
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


@pytest.fixture
def app_and_env(tmp_path):
    cfg = _settings(tmp_path)
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))
    env_file = tmp_path / ".env"
    app = create_app(cfg, clock=FakeClock(NOW), env_file=env_file)
    return app, env_file


def test_setup_page_shows_not_set_on_a_fresh_env(app_and_env) -> None:
    app, _ = app_and_env
    client = TestClient(app)

    resp = client.get("/setup")

    assert resp.status_code == 200
    assert "not set" in resp.text


def test_saving_keys_writes_env_file_and_redirects(app_and_env) -> None:
    app, env_file = app_and_env
    client = TestClient(app)

    resp = client.post(
        "/setup/alpaca",
        data={"api_key": "AK123", "api_secret": "topsecret"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup?saved=1"
    content = env_file.read_text()
    assert "CLAV_ALPACA__API_KEY=AK123" in content
    assert "CLAV_ALPACA__API_SECRET=topsecret" in content


def test_saved_page_confirms_configured_status_without_leaking_secret(app_and_env) -> None:
    app, _ = app_and_env
    client = TestClient(app)
    client.post("/setup/alpaca", data={"api_key": "AK123", "api_secret": "topsecret"})

    resp = client.get("/setup?saved=1")

    assert "configured" in resp.text
    assert "AK123" not in resp.text
    assert "topsecret" not in resp.text


def test_saving_only_the_key_leaves_secret_untouched(app_and_env) -> None:
    app, env_file = app_and_env
    client = TestClient(app)
    client.post("/setup/alpaca", data={"api_key": "AK1", "api_secret": "first-secret"})

    client.post("/setup/alpaca", data={"api_key": "AK2", "api_secret": ""})

    content = env_file.read_text()
    assert "CLAV_ALPACA__API_KEY=AK2" in content
    assert "CLAV_ALPACA__API_SECRET=first-secret" in content


def test_submitting_blank_form_is_rejected(app_and_env) -> None:
    app, env_file = app_and_env
    client = TestClient(app)

    resp = client.post("/setup/alpaca", data={"api_key": "", "api_secret": ""})

    assert resp.status_code == 422
    assert "Enter a key" in resp.text
    assert not env_file.exists()


def test_token_gated_when_configured(tmp_path) -> None:
    cfg = _settings(tmp_path, token="s3cret")
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))
    env_file = tmp_path / ".env"
    app = create_app(cfg, clock=FakeClock(NOW), env_file=env_file)
    client = TestClient(app)

    resp = client.post("/setup/alpaca", data={"api_key": "AK1", "api_secret": "s"})
    assert resp.status_code == 401
    assert not env_file.exists()

    resp = client.post(
        "/setup/alpaca", data={"api_key": "AK1", "api_secret": "s", "_token": "s3cret"}
    )
    assert resp.status_code in (200, 303)
    assert env_file.exists()
