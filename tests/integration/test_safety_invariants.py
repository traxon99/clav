"""Story 1.15 — the four Epic-1 safety invariants CI must enforce
(docs/11-testing.md §3). Each is proven directly here for auditability, even
though some are also exercised incidentally elsewhere (test_risk_engine.py's
hypothesis properties, test_execution.py's idempotency tests, etc.) — a
safety-critical invariant deserves one file a reviewer can open and see all
four proven, rather than being inferred from scattered component tests.

1. No order is ever submitted without a passing RiskDecision.
2. emergency_stop or paused => zero new entries.
3. No two orders share a client_order_id.
4. Live mode is fail-closed (Epic 6, epic-06 decision #1): unreachable without
   the deliberate two-key opt-in (config flag + live credentials); a fresh
   clone stays on paper by default.
"""

from datetime import UTC, datetime, time
from unittest.mock import MagicMock

import pytest
import yaml
from sqlalchemy.exc import IntegrityError

from clav.clock import FakeClock
from clav.common.errors import ConfigError
from clav.config import load_settings
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.models import (
    MarketClock,
    OrderRequest,
    PortfolioSnapshot,
    RiskDecision,
    TradeDecision,
)
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import RiskContext, TradingWindow, default_rules
from clav.integrations.broker_factory import broker_factory
from clav.interfaces.broker import Broker
from clav.services.execution import ExecutionEngine

NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _decision(action: str = "BUY") -> TradeDecision:
    return TradeDecision(
        cycle_id="cycle-1",
        symbol="AAPL",
        action=action,  # type: ignore[arg-type]
        target_qty=10,
        raw_score=0.5,
        technical_score=0.5,
        llm_signal=0.0,
        portfolio_bias=0.0,
    )


# --- Invariant 1: no order without a passing RiskDecision -----------------


def test_invariant_1_no_order_without_a_passing_risk_decision(session_factory) -> None:
    broker = MagicMock(spec=Broker)
    broker.get_clock.return_value = MarketClock(
        timestamp=NOW, is_open=True, next_open=NOW, next_close=NOW
    )

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        engine = ExecutionEngine(broker, repos, clock=FakeClock(NOW))

        not_approved = RiskDecision(approved=False, adjusted_qty=0, blocked_by=["SomeRule"])
        result = engine.execute(_decision(), not_approved)

        assert result is None
        broker.submit_order.assert_not_called()
        assert repos.orders.get_open_orders() == []


# --- Invariant 2: emergency_stop/paused => zero new entries ---------------


@pytest.mark.parametrize("flag_key", ["emergency_stop", "paused"])
def test_invariant_2_estop_or_paused_blocks_every_new_entry(session_factory, flag_key) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        repos.system_control.set(flag_key, "true", updated_at=NOW, updated_by="test")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        flag_value = repos.system_control.get(flag_key) == "true"

    ctx = RiskContext(
        decision=_decision("BUY"),
        portfolio=PortfolioSnapshot(ts=NOW, cash=100_000, equity=100_000, buying_power=100_000),
        price=100.0,
        now=NOW,
        market_open=True,
        trading_window=TradingWindow(start=time(9, 35), end=time(15, 55)),
        max_position_value=100_000,
        buying_power_buffer_pct=0.0,
        emergency_stop=flag_value if flag_key == "emergency_stop" else False,
        paused=flag_value if flag_key == "paused" else False,
        daily_start_equity=None,
        max_daily_loss_pct=1.0,
        max_drawdown_pct=1.0,
        max_portfolio_exposure_pct=1.0,
        sector="unknown",
        max_sector_allocation_pct=1.0,
        data_stale=False,
        avg_volume=1_000_000.0,
        min_avg_volume=0.0,
        earnings_blackout=False,
        cooldown_active=False,
    )
    risk_decision = RiskEngine(default_rules()).evaluate(ctx)

    assert risk_decision.approved is False


# --- Invariant 3: no two orders share a client_order_id --------------------


def test_invariant_3_client_order_id_is_globally_unique(session_factory) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        req = OrderRequest(client_order_id="clav-c1-AAPL-buy", symbol="AAPL", side="buy", qty=8)
        repos.orders.create(
            instrument_id=instrument.id, decision_id=None, request=req, submitted_at=NOW
        )

    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_by_symbol("AAPL")
        repos.orders.create(
            instrument_id=instrument.id, decision_id=None, request=req, submitted_at=NOW
        )


# --- Invariant 4: live mode is fail-closed (epic-06 decision #1) -----------


def test_invariant_4_config_rejects_live_mode_without_flag(tmp_path, monkeypatch) -> None:
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(yaml.safe_dump({"mode": "live", "watchlist": ["AAPL"], "alpaca": {}}))
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret")

    with pytest.raises(ConfigError, match="i_understand_live_trading"):
        load_settings(env_file=tmp_path / "does-not-exist.env")


def test_invariant_4_broker_factory_rejects_live_mode_without_keys() -> None:
    with pytest.raises(ValueError, match="live_api_key"):
        broker_factory("live", clock=FakeClock(NOW))


def test_invariant_4_live_mode_reachable_only_with_both_keys(tmp_path, monkeypatch) -> None:
    # Proves the gate is fail-closed in *both* directions (epic-06 acceptance
    # demo): missing either key refuses; both present actually works.
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "mode": "live",
                "i_understand_live_trading": True,
                "watchlist": ["AAPL"],
                "alpaca": {},
            }
        )
    )
    monkeypatch.setenv("CLAV_CONFIG_FILE", str(yaml_path))
    monkeypatch.setenv("CLAV_ALPACA__API_KEY", "key")
    monkeypatch.setenv("CLAV_ALPACA__API_SECRET", "secret")
    monkeypatch.setenv("CLAV_ALPACA_LIVE__API_KEY", "live-key")
    monkeypatch.setenv("CLAV_ALPACA_LIVE__API_SECRET", "live-secret")

    settings = load_settings(env_file=tmp_path / "does-not-exist.env")

    assert settings.mode == "live"
    broker = broker_factory(
        settings.mode,
        clock=FakeClock(NOW),
        live_api_key=settings.alpaca_live.api_key.get_secret_value(),
        live_api_secret=settings.alpaca_live.api_secret.get_secret_value(),
    )
    assert broker is not None
