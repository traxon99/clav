import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

REPO_ROOT = Path(__file__).resolve().parents[2]

EPIC_1_TABLES = {
    "instrument",
    "candle",
    "indicator_set",
    "scan_cycle",
    "decision",
    "order",
    "fill",
    "trade",
    "position",
    "portfolio_snapshot",
    "system_control",
    "audit_log",
}


def _alembic_config(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("CLAV_DB_PATH", str(db_path))
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


def test_upgrade_head_then_downgrade_base_is_clean(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "clav.db"
    cfg = _alembic_config(db_path, monkeypatch)

    command.upgrade(cfg, "head")
    con = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        assert tables >= EPIC_1_TABLES
    finally:
        con.close()

    command.downgrade(cfg, "base")
    con = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        assert tables == {"alembic_version"}
    finally:
        con.close()


def test_client_order_id_unique_constraint(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "clav.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "insert into instrument (symbol, asset_class, is_active) values ('AAPL','us_equity',1)"
        )
        con.execute(
            'insert into "order" (instrument_id, client_order_id, side, type, qty, status) '
            "values (1, 'clav-c1-AAPL-buy', 'buy', 'market', 10, 'new')"
        )
        con.commit()
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                'insert into "order" (instrument_id, client_order_id, side, type, qty, status) '
                "values (1, 'clav-c1-AAPL-buy', 'buy', 'market', 5, 'new')"
            )
    finally:
        con.close()


def test_candle_unique_constraint(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "clav.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "insert into instrument (symbol, asset_class, is_active) values ('AAPL','us_equity',1)"
        )
        con.execute(
            "insert into candle (instrument_id, timeframe, open, high, low, close, volume, ts) "
            "values (1, '1Day', 1, 2, 0.5, 1.5, 100, '2025-01-01T00:00:00')"
        )
        con.commit()
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "insert into candle (instrument_id, timeframe, open, high, low, close, volume, ts) "
                "values (1, '1Day', 1, 2, 0.5, 1.5, 100, '2025-01-01T00:00:00')"
            )
    finally:
        con.close()
