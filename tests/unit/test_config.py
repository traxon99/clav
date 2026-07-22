from datetime import UTC, datetime, time
from pathlib import Path

import pytest
import yaml

from clav.common.errors import ConfigError
from clav.config import load_settings
from clav.domain.models import PortfolioSnapshot, TradeDecision
from clav.domain.risk.rules import MaxPositionSizeRule, RiskContext, TradingWindow

REPO_ROOT = Path(__file__).resolve().parents[2]

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
    assert settings.sector_map == {}
    assert settings.earnings_calendar == []


def test_sector_map_defaults_empty_and_normalizes_symbol_case(
    tmp_path, monkeypatch, missing_env_file
) -> None:
    with_map = {**VALID_YAML, "sector_map": {"aapl": "Technology", "XOM": "Energy"}}
    yaml_path = _write_yaml(tmp_path / "config.yaml", with_map)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key123")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret456")

    settings = load_settings(env_file=missing_env_file)

    assert settings.sector_map == {"AAPL": "Technology", "XOM": "Energy"}


def test_earnings_calendar_defaults_empty_and_normalizes_symbol_case(
    tmp_path, monkeypatch, missing_env_file
) -> None:
    with_calendar = {
        **VALID_YAML,
        "earnings_calendar": [
            {"symbol": "aapl", "scheduled_at": "2025-07-01T00:00:00Z", "confirmed": True},
        ],
    }
    yaml_path = _write_yaml(tmp_path / "config.yaml", with_calendar)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key123")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret456")

    settings = load_settings(env_file=missing_env_file)

    assert settings.earnings_calendar[0].symbol == "AAPL"
    assert settings.earnings_calendar[0].confirmed is True
    assert settings.earnings_calendar[0].event_type == "earnings"


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


def test_live_mode_without_flag_is_rejected(tmp_path, monkeypatch, missing_env_file) -> None:
    bad = {**VALID_YAML, "mode": "live"}
    yaml_path = _write_yaml(tmp_path / "config.yaml", bad)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key123")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret456")

    with pytest.raises(ConfigError, match="i_understand_live_trading"):
        load_settings(env_file=missing_env_file)


def test_live_mode_with_flag_passes_config_gate(tmp_path, monkeypatch, missing_env_file) -> None:
    # Story 6.1: the config-level gate only checks the flag — live credential
    # presence is broker_factory's job (checked separately at broker
    # construction, not at Settings-load time).
    good = {**VALID_YAML, "mode": "live", "i_understand_live_trading": True}
    yaml_path = _write_yaml(tmp_path / "config.yaml", good)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key123")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret456")

    settings = load_settings(env_file=missing_env_file)

    assert settings.mode == "live"
    assert settings.i_understand_live_trading is True


def test_paper_mode_with_flag_stays_paper(tmp_path, monkeypatch, missing_env_file) -> None:
    # The flag is inert without mode: live.
    with_flag = {**VALID_YAML, "mode": "paper", "i_understand_live_trading": True}
    yaml_path = _write_yaml(tmp_path / "config.yaml", with_flag)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key123")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret456")

    settings = load_settings(env_file=missing_env_file)

    assert settings.mode == "paper"


def test_fresh_clone_default_stays_paper(tmp_path, monkeypatch, missing_env_file) -> None:
    yaml_path = _write_yaml(tmp_path / "config.yaml", VALID_YAML)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key123")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret456")

    settings = load_settings(env_file=missing_env_file)

    assert settings.mode == "paper"
    assert settings.i_understand_live_trading is False
    assert settings.alpaca_live.api_key is None
    assert settings.alpaca_live.api_secret is None


def test_live_credentials_load_from_env_separately_from_paper(
    tmp_path, monkeypatch, missing_env_file
) -> None:
    good = {**VALID_YAML, "mode": "live", "i_understand_live_trading": True}
    yaml_path = _write_yaml(tmp_path / "config.yaml", good)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "paper-key")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "paper-secret")
    monkeypatch.setenv("CLAV_ALPACA_LIVE__API_KEY", "live-key")
    monkeypatch.setenv("CLAV_ALPACA_LIVE__API_SECRET", "live-secret")

    settings = load_settings(env_file=missing_env_file)

    assert settings.alpaca.api_key.get_secret_value() == "paper-key"
    assert settings.alpaca_live.api_key.get_secret_value() == "live-key"
    assert settings.alpaca_live.api_secret.get_secret_value() == "live-secret"


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


def test_risk_config_epic2_defaults_are_sane(tmp_path, monkeypatch, missing_env_file) -> None:
    yaml_path = _write_yaml(tmp_path / "config.yaml", VALID_YAML)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key123")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret456")

    settings = load_settings(env_file=missing_env_file)

    assert settings.risk.risk_fraction == 0.01
    assert settings.risk.atr_stop_mult == 2.0
    assert settings.risk.take_profit_mult == 2.0
    assert settings.risk.max_daily_loss_pct == 0.03
    assert settings.risk.max_drawdown_pct == 0.10
    assert settings.risk.max_portfolio_exposure_pct == 0.80
    assert settings.risk.max_sector_allocation_pct == 0.30
    assert settings.risk.earnings_blackout_days == 2
    assert settings.risk.cooldown_minutes == 60
    assert settings.risk.post_loss_cooldown_minutes == 120
    assert settings.risk.min_avg_volume == 100_000.0
    assert settings.risk.quote_staleness_seconds == 300
    assert settings.risk.flatten_on_estop is False


@pytest.mark.parametrize(
    "bad_field,bad_value",
    [
        ("risk_fraction", 0.0),
        ("risk_fraction", 1.0),
        ("atr_stop_mult", 0.0),
        ("take_profit_mult", 0.0),
        ("max_daily_loss_pct", 0.0),
        ("max_daily_loss_pct", 1.0),
        ("max_drawdown_pct", 0.0),
        ("max_portfolio_exposure_pct", 0.0),
        ("max_portfolio_exposure_pct", 1.5),
        ("max_sector_allocation_pct", 0.0),
        ("min_avg_volume", -1.0),
        ("quote_staleness_seconds", 0),
        ("earnings_blackout_days", -1),
        ("cooldown_minutes", -1),
        ("post_loss_cooldown_minutes", -1),
    ],
)
def test_risk_config_out_of_range_values_rejected(
    tmp_path, monkeypatch, missing_env_file, bad_field, bad_value
) -> None:
    bad = {**VALID_YAML, "risk": {bad_field: bad_value}}
    yaml_path = _write_yaml(tmp_path / "config.yaml", bad)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key123")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret456")

    with pytest.raises(ConfigError):
        load_settings(env_file=missing_env_file)


def test_review_config_defaults_are_sane(tmp_path, monkeypatch, missing_env_file) -> None:
    """epic-05 Story 5.7: a fresh clone gets an off-peak-friendly review
    cadence with no `review:` block configured at all."""
    yaml_path = _write_yaml(tmp_path / "config.yaml", VALID_YAML)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key123")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret456")

    settings = load_settings(env_file=missing_env_file)

    assert settings.review.interval_minutes == 120
    assert settings.review.max_attempts == 5
    assert settings.review.backoff_base_seconds == 300.0
    assert settings.review.backoff_max_seconds == 21_600.0


@pytest.mark.parametrize(
    "bad_field,bad_value",
    [
        ("interval_minutes", 0),
        ("max_attempts", 0),
        ("backoff_base_seconds", 0.0),
        ("backoff_max_seconds", 0.0),
    ],
)
def test_review_config_out_of_range_values_rejected(
    tmp_path, monkeypatch, missing_env_file, bad_field, bad_value
) -> None:
    bad = {**VALID_YAML, "review": {bad_field: bad_value}}
    yaml_path = _write_yaml(tmp_path / "config.yaml", bad)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key123")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret456")

    with pytest.raises(ConfigError):
        load_settings(env_file=missing_env_file)


def test_review_config_backoff_base_greater_than_max_rejected(
    tmp_path, monkeypatch, missing_env_file
) -> None:
    bad = {
        **VALID_YAML,
        "review": {"backoff_base_seconds": 100.0, "backoff_max_seconds": 50.0},
    }
    yaml_path = _write_yaml(tmp_path / "config.yaml", bad)
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key123")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret456")

    with pytest.raises(ConfigError):
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


# --- Story 6.5: config/config.pilot.example.yaml ----------------------------


def test_pilot_profile_loads_live_with_tight_caps(monkeypatch, missing_env_file) -> None:
    pilot_yaml = REPO_ROOT / "config" / "config.pilot.example.yaml"
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(pilot_yaml))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "paper-key")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "paper-secret")
    monkeypatch.setenv("CLAV_ALPACA_LIVE__API_KEY", "live-key")
    monkeypatch.setenv("CLAV_ALPACA_LIVE__API_SECRET", "live-secret")

    settings = load_settings(env_file=missing_env_file)

    assert settings.mode == "live"
    assert settings.i_understand_live_trading is True
    assert settings.watchlist == ["AAPL", "MSFT"]
    # tighter than config.example.yaml's paper defaults (2000 / 0.03 / 0.10)
    assert settings.risk.max_position_value == 100.0
    assert settings.risk.max_daily_loss_pct == 0.01
    assert settings.risk.max_drawdown_pct == 0.03
    assert settings.risk.flatten_on_estop is True


def test_pilot_profiles_max_position_value_actually_shrinks_a_larger_order(
    monkeypatch, missing_env_file
) -> None:
    """Not just a config value — proves the existing MaxPositionSizeRule
    (no new limit code, epic-06 decision #5) actually caps a candidate order
    down using the pilot's tight max_position_value."""
    pilot_yaml = REPO_ROOT / "config" / "config.pilot.example.yaml"
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(pilot_yaml))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "paper-key")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "paper-secret")
    monkeypatch.setenv("CLAV_ALPACA_LIVE__API_KEY", "live-key")
    monkeypatch.setenv("CLAV_ALPACA_LIVE__API_SECRET", "live-secret")
    settings = load_settings(env_file=missing_env_file)

    price = 50.0  # a candidate 100-share BUY would be a $5,000 notional
    decision = TradeDecision(
        cycle_id="cycle-1",
        symbol="AAPL",
        action="BUY",
        target_qty=100,
        raw_score=0.5,
        technical_score=0.5,
        llm_signal=0.0,
        portfolio_bias=0.0,
    )
    ctx = RiskContext(
        decision=decision,
        portfolio=PortfolioSnapshot(
            ts=datetime(2026, 1, 2, 16, 0, tzinfo=UTC),
            cash=10_000,
            equity=10_000,
            buying_power=10_000,
        ),
        price=price,
        now=datetime(2026, 1, 2, 16, 0, tzinfo=UTC),
        market_open=True,
        trading_window=TradingWindow(start=time(9, 35), end=time(15, 55)),
        max_position_value=settings.risk.max_position_value,
        buying_power_buffer_pct=settings.risk.buying_power_buffer_pct,
        emergency_stop=False,
        paused=False,
        daily_start_equity=None,
        max_daily_loss_pct=settings.risk.max_daily_loss_pct,
        max_drawdown_pct=settings.risk.max_drawdown_pct,
        max_portfolio_exposure_pct=settings.risk.max_portfolio_exposure_pct,
        sector="Technology",
        max_sector_allocation_pct=settings.risk.max_sector_allocation_pct,
        data_stale=False,
        avg_volume=1_000_000.0,
        min_avg_volume=settings.risk.min_avg_volume,
        earnings_blackout=False,
        cooldown_active=False,
    )

    outcome = MaxPositionSizeRule().apply(ctx)

    assert outcome.passed is True
    # $100 cap / $50 price = 2 shares -- far below the requested 100
    assert outcome.max_qty == 2
    assert outcome.max_qty < decision.target_qty
