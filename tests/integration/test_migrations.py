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

EPIC_2_TABLES = {
    "risk_evaluation",
    "earnings_event",
}

EPIC_3_TABLES = {
    "news_item",
    "social_digest",
    "trade_proposal",
    "prompt_version",
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
        assert tables >= EPIC_1_TABLES | EPIC_2_TABLES | EPIC_3_TABLES
    finally:
        con.close()

    command.downgrade(cfg, "base")
    con = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        assert tables == {"alembic_version"}
    finally:
        con.close()


def test_upgrade_creates_missing_data_dir(tmp_path, monkeypatch) -> None:
    """Fresh-clone flow: `alembic upgrade head` runs before anything creates ./data."""
    db_path = tmp_path / "data" / "clav.db"
    cfg = _alembic_config(db_path, monkeypatch)

    command.upgrade(cfg, "head")
    assert db_path.exists()


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


def test_risk_evaluation_links_to_decision(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "clav.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "insert into instrument (symbol, asset_class, is_active) values ('AAPL','us_equity',1)"
        )
        con.execute(
            "insert into scan_cycle (id, started_at, mode, market_open, trigger, status) "
            "values ('c1', '2025-01-01T00:00:00', 'dryrun', 1, 'scheduled', 'running')"
        )
        con.execute(
            "insert into decision (scan_cycle_id, instrument_id, action, raw_score, "
            "technical_score, llm_signal, portfolio_bias, target_qty, reasoning, created_at) "
            "values ('c1', 1, 'BUY', 0.5, 0.5, 0, 0, 10, '{}', '2025-01-01T00:00:00')"
        )
        con.execute(
            "insert into risk_evaluation (decision_id, approved, adjusted_qty, blocked_by, "
            "notes, evaluated_at) values (1, 1, 10, '[]', '{}', '2025-01-01T00:00:00')"
        )
        con.commit()
        row = con.execute(
            "select decision_id, approved, adjusted_qty from risk_evaluation"
        ).fetchone()
        assert row == (1, 1, 10)
    finally:
        con.close()


def test_earnings_event_links_to_instrument(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "clav.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "insert into instrument (symbol, asset_class, is_active) values ('AAPL','us_equity',1)"
        )
        con.execute(
            "insert into earnings_event (instrument_id, event_type, scheduled_at, confirmed, "
            "source) values (1, 'quarterly', '2025-02-01T00:00:00', 0, 'seed')"
        )
        con.commit()
        row = con.execute(
            "select instrument_id, event_type, confirmed from earnings_event"
        ).fetchone()
        assert row == (1, "quarterly", 0)
    finally:
        con.close()


def test_news_item_content_hash_unique_constraint(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "clav.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "insert into instrument (symbol, asset_class, is_active) values ('AAPL','us_equity',1)"
        )
        con.execute(
            "insert into news_item (instrument_id, content_hash, external_id, source, headline, "
            "body, published_at, fetched_at) values "
            "(1, 'hash-abc', 'ext1', 'rss', 'headline', '', "
            "'2026-07-01T00:00:00', '2026-07-01T00:00:00')"
        )
        con.commit()
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "insert into news_item (instrument_id, content_hash, external_id, source, "
                "headline, body, published_at, fetched_at) values "
                "(1, 'hash-abc', 'ext2', 'edgar', 'same story', '', "
                "'2026-07-02T00:00:00', '2026-07-02T00:00:00')"
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
