import pytest

from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.tables import Base, Instrument


def test_make_engine_sets_wal_pragmas(tmp_path) -> None:
    engine = make_engine(tmp_path / "clav.db")
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        fk = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
        timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()

    assert mode == "wal"
    assert fk == 1
    assert timeout == 5000


def test_session_scope_commits_on_success(tmp_path) -> None:
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    with session_scope(factory) as session:
        session.add(Instrument(symbol="AAPL"))

    with session_scope(factory) as session:
        row = session.query(Instrument).filter_by(symbol="AAPL").one()
        assert row.symbol == "AAPL"


def test_session_scope_rolls_back_on_error(tmp_path) -> None:
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    with pytest.raises(RuntimeError), session_scope(factory) as session:
        session.add(Instrument(symbol="AAPL"))
        raise RuntimeError("boom")

    with session_scope(factory) as session:
        assert session.query(Instrument).count() == 0
