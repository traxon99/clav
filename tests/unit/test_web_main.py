"""``run_web`` turns a raw "address already in use" bind failure into an
actionable operator message instead of a bare asyncio traceback -- the
common case being a leftover/duplicate clav-web instance still holding the
port (see ``clav.web.main._check_port_available``).

uvicorn's own ``Server.startup()`` catches the EADDRINUSE ``OSError``
itself, logs it via its own logger, and calls ``sys.exit()`` -- which
raises ``SystemExit``, not ``OSError``. A ``try/except OSError`` around
``uvicorn.run()`` alone never actually catches this in practice, so
``run_web()`` checks the port itself, with a real socket, before ever
calling into uvicorn -- these tests bind a real port rather than mocking
uvicorn's behavior, so they can't drift out of sync with what uvicorn
actually does the way the previous version of this test could (and did)."""

from __future__ import annotations

import errno
import socket

import pytest

from clav.config import Settings
from clav.data.db import make_engine
from clav.data.tables import Base
from clav.web import main as web_main


def _settings(tmp_path, *, port: int) -> Settings:
    Base.metadata.create_all(make_engine(tmp_path / "clav.db"))
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        watchlist=["AAPL"],
        alpaca={"api_key": "k", "api_secret": "s"},
        data_dir=tmp_path,
        web={"bind_host": "127.0.0.1", "bind_port": port},
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_check_port_available_passes_when_port_is_free() -> None:
    port = _free_port()
    web_main._check_port_available("127.0.0.1", port)  # must not raise


def test_check_port_available_raises_actionable_system_exit_when_taken() -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        with pytest.raises(SystemExit) as exc_info:
            web_main._check_port_available("127.0.0.1", port)
        message = str(exc_info.value)
        assert "already in use" in message
        assert "already running" in message
        assert str(port) in message
    finally:
        holder.close()


def test_run_web_raises_actionable_system_exit_before_ever_calling_uvicorn(
    tmp_path, monkeypatch
) -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        monkeypatch.setattr(web_main, "load_settings", lambda: _settings(tmp_path, port=port))

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("uvicorn.run should never be reached -- port is taken")

        monkeypatch.setattr(web_main.uvicorn, "run", _fail_if_called)

        with pytest.raises(SystemExit) as exc_info:
            web_main.run_web()
        assert "already in use" in str(exc_info.value)
    finally:
        holder.close()


def test_run_web_starts_normally_when_port_is_free(tmp_path, monkeypatch) -> None:
    port = _free_port()
    monkeypatch.setattr(web_main, "load_settings", lambda: _settings(tmp_path, port=port))

    called = {}

    def _record(*args, **kwargs):
        called["host"] = kwargs.get("host")
        called["port"] = kwargs.get("port")

    monkeypatch.setattr(web_main.uvicorn, "run", _record)

    web_main.run_web()

    assert called == {"host": "127.0.0.1", "port": port}


def test_other_os_errors_from_uvicorn_are_not_swallowed(tmp_path, monkeypatch) -> None:
    port = _free_port()
    monkeypatch.setattr(web_main, "load_settings", lambda: _settings(tmp_path, port=port))

    def _raise_other(*args, **kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(web_main.uvicorn, "run", _raise_other)

    with pytest.raises(OSError) as exc_info:
        web_main.run_web()
    assert exc_info.value.errno == errno.EACCES
