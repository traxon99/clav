"""AlpacaBroker — the live-trading counterpart to PaperBroker (Story 6.1/6.2,
epic-06 decision #2). Constructed **only** by ``broker_factory`` once the
two-key live gate (config flag + live credentials) has passed. Differs from
``PaperBroker`` only in which endpoint/key pair the underlying
``TradingClient`` authenticates with (``paper=False``); every order/status/
error mapping rule is shared via ``AlpacaBrokerBase`` so a live-only bug
can't hide in a forked copy.
"""

from __future__ import annotations

from alpaca.trading.client import TradingClient

from clav.integrations._alpaca_broker_base import AlpacaBrokerBase


class AlpacaBroker(AlpacaBrokerBase):
    def __init__(
        self, api_key: str, api_secret: str, *, client: TradingClient | None = None
    ) -> None:
        self._client = client or TradingClient(api_key, api_secret, paper=False)
