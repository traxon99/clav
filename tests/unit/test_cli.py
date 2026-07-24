from pathlib import Path

import yaml
from click.testing import CliRunner

from clav.cli import cli
from clav.data.db import make_engine
from clav.data.tables import Base

VALID_YAML: dict = {
    "mode": "paper",
    "watchlist": ["AAPL"],
    "alpaca": {},
}


def _write_config(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(yaml.safe_dump({**VALID_YAML, "data_dir": str(data_dir)}))
    make_engine(data_dir / "clav.db")  # ensure data_dir exists
    engine = make_engine(data_dir / "clav.db")
    Base.metadata.create_all(engine)
    return yaml_path


def _env(yaml_path: Path) -> dict[str, str]:
    return {
        "CLAV_CONFIG_FILE": str(yaml_path),
        "CLAV_ALPACA__API_KEY": "key123",
        "CLAV_ALPACA__API_SECRET": "secret456",
    }


def test_status_defaults_to_false(tmp_path) -> None:
    yaml_path = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["status"], env=_env(yaml_path))

    assert result.exit_code == 0, result.output
    assert "emergency_stop: false" in result.output
    assert "paused: false" in result.output


def test_estop_set_then_status_reflects_it(tmp_path) -> None:
    yaml_path = _write_config(tmp_path)
    runner = CliRunner()
    env = _env(yaml_path)

    set_result = runner.invoke(cli, ["estop-set"], env=env)
    assert set_result.exit_code == 0, set_result.output
    assert "emergency_stop: true" in set_result.output

    status_result = runner.invoke(cli, ["status"], env=env)
    assert "emergency_stop: true" in status_result.output


def test_estop_clear_roundtrip(tmp_path) -> None:
    yaml_path = _write_config(tmp_path)
    runner = CliRunner()
    env = _env(yaml_path)

    runner.invoke(cli, ["estop-set"], env=env)
    clear_result = runner.invoke(cli, ["estop-clear"], env=env)
    assert clear_result.exit_code == 0
    assert "emergency_stop: false" in clear_result.output

    status_result = runner.invoke(cli, ["status"], env=env)
    assert "emergency_stop: false" in status_result.output


def test_pause_resume_roundtrip(tmp_path) -> None:
    yaml_path = _write_config(tmp_path)
    runner = CliRunner()
    env = _env(yaml_path)

    runner.invoke(cli, ["pause"], env=env)
    status_result = runner.invoke(cli, ["status"], env=env)
    assert "paused: true" in status_result.output

    runner.invoke(cli, ["resume"], env=env)
    status_result = runner.invoke(cli, ["status"], env=env)
    assert "paused: false" in status_result.output


def test_soak_report_empty_window_renders_cleanly(tmp_path) -> None:
    yaml_path = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["soak-report", "--hours", "24"], env=_env(yaml_path))

    assert result.exit_code == 0, result.output
    assert "duplicate client_order_ids: 0" in result.output
    assert "CLEAN" in result.output


def test_invalid_config_produces_clean_cli_error(tmp_path) -> None:
    yaml_path = tmp_path / "bad-config.yaml"
    yaml_path.write_text(yaml.safe_dump({"mode": "paper"}))  # missing watchlist/alpaca
    runner = CliRunner()

    result = runner.invoke(cli, ["status"], env={"CLAV_CONFIG_FILE": str(yaml_path)})

    assert result.exit_code != 0
    assert "Invalid or missing configuration" in result.output
