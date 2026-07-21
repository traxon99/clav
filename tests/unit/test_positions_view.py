"""build_position_rows (shared by /portfolio and the dashboard's "Open
Positions" panel): current price (marked to the last known close), cost
average, and unrealized P&L in both $ and %. The % must be correctly signed
for a short position -- a price drop is a gain, not a loss."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from clav.data.db import make_engine, make_session_factory
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import Candle, Position
from clav.web.positions_view import build_position_rows

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _candle(symbol: str, close: float) -> Candle:
    return Candle(
        symbol=symbol,
        timeframe="1Day",
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000,
        ts=NOW,
    )


def test_long_position_pct_is_price_return(factory) -> None:
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create("AAPL")
    repos.candles.upsert_many(instrument.id, [_candle("AAPL", 195.0)])
    repos.positions.upsert(
        instrument.id, Position(symbol="AAPL", qty=10, avg_entry_price=180.0), opened_at=NOW
    )
    session.commit()

    rows = build_position_rows(repos)
    session.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["unrealized_pl"] == pytest.approx(150.0)
    assert row["unrealized_pl_pct"] == pytest.approx(150.0 / (10 * 180.0))


def test_short_position_pct_is_positive_when_price_drops(factory) -> None:
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create("MSFT")
    repos.candles.upsert_many(instrument.id, [_candle("MSFT", 380.0)])
    repos.positions.upsert(
        instrument.id, Position(symbol="MSFT", qty=-5, avg_entry_price=400.0), opened_at=NOW
    )
    session.commit()

    rows = build_position_rows(repos)
    session.close()

    row = rows[0]
    # (380 - 400) * -5 = 100 -- a short profits when price falls.
    assert row["unrealized_pl"] == pytest.approx(100.0)
    assert row["unrealized_pl_pct"] == pytest.approx(100.0 / (5 * 400.0))
    assert row["unrealized_pl_pct"] > 0


def test_short_position_pct_is_negative_when_price_rises(factory) -> None:
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create("MSFT")
    repos.candles.upsert_many(instrument.id, [_candle("MSFT", 420.0)])
    repos.positions.upsert(
        instrument.id, Position(symbol="MSFT", qty=-5, avg_entry_price=400.0), opened_at=NOW
    )
    session.commit()

    rows = build_position_rows(repos)
    session.close()

    row = rows[0]
    assert row["unrealized_pl"] == pytest.approx(-100.0)
    assert row["unrealized_pl_pct"] < 0


def test_no_candle_data_leaves_pct_none(factory) -> None:
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create("AAPL")
    repos.positions.upsert(
        instrument.id, Position(symbol="AAPL", qty=10, avg_entry_price=180.0), opened_at=NOW
    )
    session.commit()

    rows = build_position_rows(repos)
    session.close()

    row = rows[0]
    assert row["last_close"] is None
    assert row["unrealized_pl"] is None
    assert row["unrealized_pl_pct"] is None


def test_zero_qty_positions_are_excluded(factory) -> None:
    session = factory()
    repos = Repositories(session)
    instrument = repos.instruments.get_or_create("AAPL")
    repos.positions.upsert(
        instrument.id, Position(symbol="AAPL", qty=0, avg_entry_price=180.0), opened_at=NOW
    )
    session.commit()

    rows = build_position_rows(repos)
    session.close()

    assert rows == []
