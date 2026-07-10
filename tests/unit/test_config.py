from pathlib import Path

import pytest
import yaml

from clav.common.errors import ConfigError
from clav.config import load_settings

VALID_YAML: dict = {
    "mode": "paper",
    "watchlist": ["aapl", "MSFT"],
    "scan_interval_minutes": 15,
    "alpaca": {},
}


def _write_yaml(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture
def missing_env_file(tmp_path: Path) -> Path:
    # Point at a .env that doesn't exist, so real ambient env vars set by the
    # test (via monkeypatch) are the only source of secrets.
    return tmp_path / "does-not-exist.env"


def test_valid_config_loads(tmp_path, monkeypatch, missing_env_file) -> None:
    yaml_path = _write_yaml(tmp_path / "config.yaml", VALID_YAML)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key123")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret456")

    settings = load_settings(env_file=missing_env_file)

    assert settings.mode == "paper"
    assert settings.watchlist == ["AAPL", "MSFT"]  # normalized uppercase
    assert settings.alpaca.api_key.get_secret_value() == "key123"
    assert settings.scan_interval_minutes == 15


def test_missing_alpaca_secret_refuses_to_start(tmp_path, monkeypatch, missing_env_file) -> None:
    yaml_path = _write_yaml(tmp_path / "config.yaml", VALID_YAML)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.delenv("CLAV_ALPACA__API_KEY", raising=False)
    monkeypatch.delenv("CLAV_ALPACA__API_SECRET", raising=False)

    with pytest.raises(ConfigError, match="alpaca"):
        load_settings(env_file=missing_env_file)


def test_missing_watchlist_refuses_to_start(tmp_path, monkeypatch, missing_env_file) -> None:
    bad = {k: v for k, v in VALID_YAML.items() if k != "watchlist"}
    yaml_path = _write_yaml(tmp_path / "config.yaml", bad)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key123")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret456")

    with pytest.raises(ConfigError, match="watchlist"):
        load_settings(env_file=missing_env_file)


def test_live_mode_is_rejected(tmp_path, monkeypatch, missing_env_file) -> None:
    bad = {**VALID_YAML, "mode": "live"}
    yaml_path = _write_yaml(tmp_path / "config.yaml", bad)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key123")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret456")

    with pytest.raises(ConfigError, match="Epic 1"):
        load_settings(env_file=missing_env_file)


def test_duplicate_watchlist_symbol_rejected(tmp_path, monkeypatch, missing_env_file) -> None:
    bad = {**VALID_YAML, "watchlist": ["AAPL", "aapl"]}
    yaml_path = _write_yaml(tmp_path / "config.yaml", bad)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key123")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret456")

    with pytest.raises(ConfigError, match="duplicate"):
        load_settings(env_file=missing_env_file)


def test_weights_must_sum_to_one(tmp_path, monkeypatch, missing_env_file) -> None:
    bad = {**VALID_YAML, "weights": {"technical": 0.5, "llm": 0.0, "portfolio": 0.0}}
    yaml_path = _write_yaml(tmp_path / "config.yaml", bad)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key123")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret456")

    with pytest.raises(ConfigError, match=r"sum to 1\.0"):
        load_settings(env_file=missing_env_file)


def test_snapshot_redacts_secrets(tmp_path, monkeypatch, missing_env_file) -> None:
    yaml_path = _write_yaml(tmp_path / "config.yaml", VALID_YAML)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key123")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret456")

    settings = load_settings(env_file=missing_env_file)
    snapshot = settings.to_snapshot_dict()

    assert "key123" not in str(snapshot)
    assert "secret456" not in str(snapshot)
    assert snapshot["watchlist"] == ["AAPL", "MSFT"]
