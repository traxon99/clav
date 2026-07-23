"""Story 3.8 — RuntimeConfigStore: persisted, validated operator overrides."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from clav.config import (
    RiskKnobsOverride,
    RuntimeLLMOverride,
    RuntimeOverrides,
    ThresholdsConfig,
    WeightsConfig,
)
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.services.runtime_config import RuntimeConfigStore

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def test_get_with_no_override_returns_all_none(session_factory) -> None:
    store = RuntimeConfigStore()
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        override = store.get(repos)
    assert override == RuntimeOverrides()


def test_set_then_get_round_trips(session_factory) -> None:
    store = RuntimeConfigStore()
    overrides = RuntimeOverrides(
        weights=WeightsConfig(technical=0.5, llm=0.3, portfolio=0.2),
        thresholds=ThresholdsConfig(buy=0.3, sell=-0.3),
        risk=RiskKnobsOverride(
            max_position_value=3000.0,
            max_daily_loss_pct=0.05,
            max_drawdown_pct=0.15,
            max_portfolio_exposure_pct=0.7,
            max_sector_allocation_pct=0.25,
            cooldown_minutes=30,
            post_loss_cooldown_minutes=60,
        ),
        watchlist=["AAPL", "MSFT"],
        scan_interval_minutes=15,
        llm=RuntimeLLMOverride(model="gemini-3.1-flash-lite", thinking_budget=0),
    )
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        store.set(repos, overrides, now=NOW, updated_by="operator")

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        round_tripped = store.get(repos)
    assert round_tripped == overrides


def test_weights_must_sum_to_one() -> None:
    with pytest.raises(ValueError, match=r"sum to 1\.0"):
        WeightsConfig(technical=0.9, llm=0.5, portfolio=0.1)


def test_watchlist_override_rejects_duplicates_and_empty() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        RuntimeOverrides(watchlist=["AAPL", "aapl"])
    with pytest.raises(ValueError, match="empty"):
        RuntimeOverrides(watchlist=[])


def test_risk_knobs_reject_out_of_range_values() -> None:
    with pytest.raises(ValueError):
        RiskKnobsOverride(
            max_position_value=-1.0,
            max_daily_loss_pct=0.05,
            max_drawdown_pct=0.15,
            max_portfolio_exposure_pct=0.7,
            max_sector_allocation_pct=0.25,
            cooldown_minutes=30,
            post_loss_cooldown_minutes=60,
        )


def test_llm_override_rejects_empty_model_and_negative_thinking_budget() -> None:
    with pytest.raises(ValueError):
        RuntimeLLMOverride(model="", thinking_budget=0)
    with pytest.raises(ValueError):
        RuntimeLLMOverride(model="gemini-3.5-flash", thinking_budget=-1)


def test_llm_override_zero_thinking_budget_is_valid() -> None:
    override = RuntimeLLMOverride(model="gemini-3.1-flash-lite", thinking_budget=0)
    assert override.thinking_budget == 0
