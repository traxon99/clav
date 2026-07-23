"""``run_web`` turns a raw "address already in use" bind failure into an
actionable operator message instead of a bare asyncio traceback -- the
common case being a leftover/duplicate clav-web instance still holding the
port (see errno.EADDRINUSE handling in clav.web.main)."""

from __future__ import annotations

import errno

import pytest

from clav.config import Settings
from clav.data.db import make_engine
from clav.data.tables import Base
from clav.web import main as web_main


def _settings(tmp_path) -> Settings:
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        watchlist=["AAPL"],
        alpaca={"api_key": "k", "api_secret": "s"},
        data_dir=tmp_path,
        # Pinned rather than left to ambient config.yaml (gitignored, so its
        # presence/contents vary by machine) -- this test asserts on the
        # host:port that ends up in the error message.
        web={"bind_host": "127.0.0.1", "bind_port": 8080},
    )


def test_address_in_use_raises_actionable_system_exit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web_main, "load_settings", lambda: _settings(tmp_path))

    def _raise_in_use(*args, **kwargs):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(web_main.uvicorn, "run", _raise_in_use)

    with pytest.raises(SystemExit) as exc_info:
        web_main.run_web()

    message = str(exc_info.value)
    assert "already in use" in message
    assert "already running" in message


def test_other_os_errors_are_not_swallowed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web_main, "load_settings", lambda: _settings(tmp_path))

    def _raise_other(*args, **kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(web_main.uvicorn, "run", _raise_other)

    with pytest.raises(OSError) as exc_info:
        web_main.run_web()
    assert exc_info.value.errno == errno.EACCES
