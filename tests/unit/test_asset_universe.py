"""Alpaca asset-catalog: the adapter's list_assets normalization, the default
empty catalog on sources that can't enumerate, and the repository's
search/upsert/is_tradable behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from clav.clock import FakeClock
from clav.data.db import make_engine, make_session_factory
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.integrations.alpaca_data import AlpacaDataAdapter

NOW = datetime(2026, 7, 23, tzinfo=UTC)


class FakeTradingClient:
    def __init__(self, assets) -> None:
        self._assets = assets

    def get_all_assets(self, _request):
        return self._assets


def test_adapter_normalizes_assets() -> None:
    assets = [
        SimpleNamespace(symbol="AAPL", name="Apple Inc", exchange="NASDAQ",
                        tradable=True, fractionable=True),
        SimpleNamespace(symbol="BRK.B", name="Berkshire", exchange="NYSE",
                        tradable=True, fractionable=False),
        SimpleNamespace(symbol="", name="junk", exchange="X", tradable=True, fractionable=True),
    ]
    adapter = AlpacaDataAdapter(
        "k", "s", clock=FakeClock(NOW), trading_client=FakeTradingClient(assets)
    )
    out = adapter.list_assets()
    symbols = [a.symbol for a in out]
    assert symbols == ["AAPL", "BRK.B"]  # blank symbol dropped
    assert out[0].name == "Apple Inc" and out[0].fractionable is True


def test_default_source_has_empty_catalog() -> None:
    # A market-data source that doesn't override list_assets returns [] (not an
    # error) -- discovery/on-demand degrade rather than crash.
    from clav.interfaces.market_data import MarketDataSource

    class QuotesOnly(MarketDataSource):
        def get_quote(self, symbol):  # pragma: no cover - trivial
            raise NotImplementedError

        def get_candles(self, symbol, timeframe, limit):  # pragma: no cover
            raise NotImplementedError

        def get_clock(self):  # pragma: no cover
            raise NotImplementedError

    assert QuotesOnly().list_assets() == []


def test_repo_upsert_is_idempotent_and_updates(tmp_path) -> None:
    eng = make_engine(tmp_path / "a.db")
    Base.metadata.create_all(eng)
    repos = Repositories(make_session_factory(eng)())

    repos.assets.upsert_many(
        [{"symbol": "AAPL", "name": "Apple", "tradable": True}], updated_at=NOW
    )
    repos.assets.upsert_many(
        [{"symbol": "aapl", "name": "Apple Inc.", "tradable": False}], updated_at=NOW
    )
    assert repos.assets.count() == 1  # same symbol -> updated, not duplicated
    row = repos.assets.get("AAPL")
    assert row.name == "Apple Inc." and row.tradable is False


def test_repo_search_prefix_and_tradable_filter(tmp_path) -> None:
    eng = make_engine(tmp_path / "a.db")
    Base.metadata.create_all(eng)
    repos = Repositories(make_session_factory(eng)())
    repos.assets.upsert_many(
        [
            {"symbol": "AAPL", "name": "Apple", "tradable": True},
            {"symbol": "AMD", "name": "Advanced Micro", "tradable": True},
            {"symbol": "AMC", "name": "AMC", "tradable": False},
        ],
        updated_at=NOW,
    )
    got = {a.symbol for a in repos.assets.search("AM")}
    assert got == {"AMD"}  # AMC is not tradable; AAPL doesn't prefix-match "AM"
    assert repos.assets.search("") == []  # empty query never dumps the catalog
