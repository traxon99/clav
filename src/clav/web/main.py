"""clav-web entrypoint (Story 3.8): the control API (+ Story 3.9's HTMX UI),
a **separate process** from ``clav-core`` reading the same SQLite (WAL)
database. Binds to the operator's private network by default (LAN/localhost,
or a Tailscale IP for remote — epic decision #7); an optional shared token
guards state-changing requests when configured.
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from clav.clock import Clock, SystemClock
from clav.common.errors import ConfigError
from clav.common.logging import configure_logging, get_logger
from clav.config import Settings, load_settings
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


def create_app(cfg: Settings, *, clock: Clock | None = None) -> FastAPI:
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


def run_web() -> None:
    try:
        cfg = load_settings()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error:\n{exc}") from None

    configure_logging(log_dir=cfg.log_dir)
    _logger.info("clav_web_starting", bind_host=cfg.web.bind_host, bind_port=cfg.web.bind_port)

    app = create_app(cfg)
    uvicorn.run(app, host=cfg.web.bind_host, port=cfg.web.bind_port)


def main() -> None:
    run_web()


if __name__ == "__main__":
    main()
