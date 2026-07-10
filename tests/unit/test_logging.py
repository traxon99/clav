import json
import logging

from clav.common.logging import bind_cycle_id, clear_cycle_id, configure_logging, get_logger


def test_json_logs_carry_cycle_id_and_redact_secrets(tmp_path, capsys) -> None:
    configure_logging(log_dir=tmp_path, level=logging.INFO)
    logger = get_logger("test")

    bind_cycle_id("cycle-abc123")
    try:
        logger.info("order submitted", symbol="AAPL", api_key="super-secret-value")
    finally:
        clear_cycle_id()

    out = capsys.readouterr().out.strip().splitlines()
    assert out, "expected at least one JSON log line on stdout"
    record = json.loads(out[-1])

    assert record["event"] == "order submitted"
    assert record["cycle_id"] == "cycle-abc123"
    assert record["symbol"] == "AAPL"
    assert record["api_key"] == "***REDACTED***"
    assert "super-secret-value" not in out[-1]


def test_cycle_id_not_leaked_across_unrelated_log_lines(tmp_path, capsys) -> None:
    configure_logging(log_dir=tmp_path, level=logging.INFO)
    logger = get_logger("test")

    logger.info("no cycle bound yet")
    out = capsys.readouterr().out.strip().splitlines()
    record = json.loads(out[-1])
    assert "cycle_id" not in record


def test_configure_logging_writes_rotating_file(tmp_path) -> None:
    configure_logging(log_dir=tmp_path, level=logging.INFO, file_name="clav-test.log")
    logger = get_logger("test")
    logger.info("hello from file handler")

    log_file = tmp_path / "clav-test.log"
    assert log_file.exists()
    contents = log_file.read_text()
    assert "hello from file handler" in contents
