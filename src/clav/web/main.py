"""clav-web entrypoint (Story 3.8): the control API (+ Story 3.9's HTMX UI),
a **separate process** from ``clav-core`` reading the same SQLite (WAL)
database. Binds to the operator's private network by default (LAN/localhost,
or a Tailscale IP for remote — epic decision #7); an optional shared token
guards state-changing requests when configured.
"""

from __future__ import annotations

import errno
import socket
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from clav.clock import Clock, SystemClock
from clav.common.errors import ConfigError
from clav.common.logging import bind_mode, configure_logging, get_logger
from clav.config import DEFAULT_ENV_FILE, Settings, load_settings
from clav.data.db import make_engine, make_session_factory
from clav.services.prompt_store import PromptVersionStore
from clav.services.runtime_config import RuntimeConfigStore
from clav.web.routers import audit as audit_router
from clav.web.routers import calibration as calibration_router
from clav.web.routers import config as config_router
from clav.web.routers import control as control_router
from clav.web.routers import explanations as explanations_router
from clav.web.routers import health as health_router
from clav.web.routers import journal as journal_router
from clav.web.routers import portfolio as portfolio_router
from clav.web.routers import positions as positions_router
from clav.web.routers import prompt as prompt_router
from clav.web.routers import reviews as reviews_router
from clav.web.routers import ui as ui_router

_logger = get_logger(__name__)


def create_app(
    cfg: Settings, *, clock: Clock | None = None, env_file: Path = DEFAULT_ENV_FILE
) -> FastAPI:
    clock = clock or SystemClock()
    engine = make_engine(cfg.data_dir / "clav.db")
    session_factory = make_session_factory(engine)

    app = FastAPI(title="CLAV Control API", version="0.1.0")
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.clock = clock
    app.state.cfg = cfg
    app.state.web_token = cfg.web.token.get_secret_value() if cfg.web.token else None
    app.state.runtime_config = RuntimeConfigStore()
    # Story 6.x follow-up (/setup): the same .env path load_settings() itself
    # read at startup -- so the "Alpaca keys" setup page edits exactly the
    # file this process's own config came from, not some other default.
    app.state.env_file = Path(env_file)

    prompt_store = PromptVersionStore(session_factory, clock=clock)
    prompt_store.seed_default(persona=cfg.llm.default_persona)
    app.state.prompt_store = prompt_store

    app.include_router(health_router.router)
    app.include_router(journal_router.router)
    app.include_router(portfolio_router.router)
    app.include_router(explanations_router.router)
    app.include_router(audit_router.router)
    app.include_router(calibration_router.router)
    app.include_router(reviews_router.router)
    app.include_router(positions_router.router)
    app.include_router(config_router.router)
    app.include_router(prompt_router.router)
    app.include_router(control_router.router)
    app.include_router(ui_router.router)
    return app


def _address_in_use_message(host: str, port: int) -> str:
    # A bare asyncio bind traceback here just reads as "clav-web is broken"
    # -- it's almost always a leftover/duplicate instance still holding the
    # port (e.g. a prior manual `uv run clav-web` still running). Turn it
    # into the actual actionable next step.
    return (
        f"clav-web can't start: {host}:{port} is already in use. Is another "
        "clav-web instance already running? Check with "
        f"'lsof -i :{port}' (or 'systemctl status clav-web' if installed via "
        "deploy/install.sh) and stop it before starting a new one."
    )


def _check_port_available(host: str, port: int) -> None:
    """Fail fast, before uvicorn ever attempts to bind.

    uvicorn's own ``Server.startup()`` catches an EADDRINUSE ``OSError``
    itself, logs it via its own logger (that raw "ERROR: [Errno 98] ..."
    line), and calls ``sys.exit()`` -- which raises ``SystemExit``, not
    ``OSError``. A ``try/except OSError`` around ``uvicorn.run()`` therefore
    never actually catches this in practice, no matter uvicorn's version, so
    this checks the port ourselves first instead of depending on uvicorn's
    internal exception behavior.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        # SO_REUSEADDR matches asyncio's own default server-socket behavior
        # (loop.create_server binds with it on), so this probe is bound by
        # the same rules uvicorn's real bind would be -- no false positive
        # on a port only in TIME_WAIT.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        raise SystemExit(_address_in_use_message(host, port)) from None
    finally:
        probe.close()


def run_web() -> None:
    try:
        cfg = load_settings()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error:\n{exc}") from None

    configure_logging(log_dir=cfg.log_dir)
    bind_mode(cfg.mode)
    _logger.info("clav_web_starting", bind_host=cfg.web.bind_host, bind_port=cfg.web.bind_port)

    _check_port_available(cfg.web.bind_host, cfg.web.bind_port)

    app = create_app(cfg)
    try:
        uvicorn.run(app, host=cfg.web.bind_host, port=cfg.web.bind_port)
    except OSError as exc:
        # Defensive backstop for a race between the pre-flight check above
        # and uvicorn's own bind (another process grabs the port in
        # between) -- same friendly message, just reached a different way.
        if exc.errno != errno.EADDRINUSE:
            raise
        raise SystemExit(_address_in_use_message(cfg.web.bind_host, cfg.web.bind_port)) from None


def main() -> None:
    run_web()


if __name__ == "__main__":
    main()
