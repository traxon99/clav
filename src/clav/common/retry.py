"""Shared retry-with-backoff helper distinguishing transient vs permanent
vendor errors (docs/08-project-structure.md: common/retry.py). Used by any
integration adapter that talks to a remote API over HTTP — Alpaca (``requests``)
and the Epic-3 news/social adapters (``httpx``)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import httpx
import requests
import tenacity
from alpaca.common.exceptions import APIError

_F = TypeVar("_F", bound=Callable[..., object])


def is_transient_error(exc: BaseException) -> bool:
    if isinstance(exc, requests.exceptions.Timeout | requests.exceptions.ConnectionError):
        return True
    if isinstance(exc, APIError):
        return exc.status_code is None or exc.status_code == 429 or exc.status_code >= 500
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def retry_transient(*, attempts: int = 3, max_wait: float = 5.0) -> Callable[[_F], _F]:
    return tenacity.retry(
        retry=tenacity.retry_if_exception(is_transient_error),
        stop=tenacity.stop_after_attempt(attempts),
        wait=tenacity.wait_exponential(multiplier=0.5, max=max_wait),
        reraise=True,
    )
