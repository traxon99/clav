"""PaperBroker — wraps Alpaca's paper trading endpoint via alpaca-py's
TradingClient (Story 1.6). This is the only module allowed to import the
alpaca SDK for order execution; domain/interfaces never see it.

The request/response mapping is shared with the live ``AlpacaBroker``
(Story 6.1/6.2) via ``AlpacaBrokerBase`` — see ``_alpaca_broker_base.py``.
"""

from __future__ import annotations

from alpaca.trading.client import TradingClient

from clav.integrations._alpaca_broker_base import AlpacaBrokerBase


class PaperBroker(AlpacaBrokerBase):
    def __init__(
        self, api_key: str, api_secret: str, *, client: TradingClient | None = None
    ) -> None:
        self._client = client or TradingClient(api_key, api_secret, paper=True)
