"""Structured JSON logging: rotating file + stdout (journald captures stdout under
systemd), secret redaction, and cycle_id correlation via structlog contextvars.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any

import structlog

_SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|api[_-]?secret|secret|password|token|authorization)", re.IGNORECASE
)
_REDACTED = "***REDACTED***"


def _redact_secrets(
    logger: Any, method_name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    for key in list(event_dict):
        if key != "event" and _SECRET_KEY_PATTERN.search(key):
            event_dict[key] = _REDACTED
    return event_dict


def bind_cycle_id(cycle_id: str) -> None:
    """Bind a scan-cycle correlation id to every log line emitted on this context."""
    structlog.contextvars.bind_contextvars(cycle_id=cycle_id)


def clear_cycle_id() -> None:
    structlog.contextvars.unbind_contextvars("cycle_id")


def bind_mode(mode: str) -> None:
    """Bind the trading mode (paper/dryrun/live) to every log line for the
    life of the process (Story 6.4) — unlike ``cycle_id`` this never changes
    after boot, so it's bound once at startup, not per-cycle/per-request."""
    structlog.contextvars.bind_contextvars(mode=mode)


def configure_logging(
    *, log_dir: Path, level: int = logging.INFO, file_name: str = "clav.log"
) -> None:
    """Configure stdlib logging + structlog. Call once at process startup."""
    log_dir.mkdir(parents=True, exist_ok=True)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_secrets,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )

    stdout_handler = logging.StreamHandler(stream=sys.stdout)
    stdout_handler.setFormatter(formatter)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / file_name, maxBytes=10_000_000, backupCount=5
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [stdout_handler, file_handler]
    root.setLevel(level)


def get_logger(*args: Any, **kwargs: Any) -> Any:
    return structlog.get_logger(*args, **kwargs)
