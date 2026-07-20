"""Shared HTTP text fetcher for the news adapters.

Adapters depend on the small ``TextFetcher`` protocol (``get(url) -> str``) so
tests can inject a fixture-returning callable and **no live network touches CI**.
The default ``HttpTextFetcher`` is ``httpx`` + the shared retry/backoff helper,
with a declared User-Agent (SEC EDGAR requires one).
"""

from __future__ import annotations

from typing import Protocol

import httpx

from clav.common.retry import retry_transient

DEFAULT_USER_AGENT = "CLAV/0.1 (personal paper-trading research; contact via config)"


class TextFetcher(Protocol):
    def get(self, url: str, *, headers: dict[str, str] | None = None) -> str:  # pragma: no cover
        ...


class HttpTextFetcher:
    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )

    @retry_transient()
    def get(self, url: str, *, headers: dict[str, str] | None = None) -> str:
        merged = {"User-Agent": self._user_agent, **(headers or {})}
        resp = self._client.get(url, headers=merged)
        resp.raise_for_status()
        return resp.text
