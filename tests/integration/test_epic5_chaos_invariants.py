"""Story 5.8 — the Epic-5 chaos & isolation invariant CI must enforce
(docs/epics/epic-05-trade-review-and-calibration.md, Story 5.8). Mirrors the
rationale of Epic 3's test_epic3_chaos_invariants.py: a safety-critical
invariant deserves one file a reviewer can open and see it proven, even
though the pieces are also exercised incidentally elsewhere
(test_gemini_review.py, test_trade_review_service.py).

The invariant: **every `Analyst.review()` failure mode leaves a concurrently
scheduled scan cycle trading normally.** `TradeReviewService.run_pass()` and
`ScanCycleService` share one `GeminiAnalyst`/`GeminiBudget` (epic-05 decision
#3, matching the real `app.py` composition), so this proves the sharing
itself is safe: a failing or budget-exhausted review pass never raises out
of `run_pass()`, never writes to `decision`/`risk_evaluation`/`order`, and
never leaves the shared budget/breaker in a state that stops a scan cycle
from completing before *and* after it runs.
"""

from __future__ import annotations

from datetime import time

import pytest
from conftest import NOON_UTC, FakeMarketDataSource
from conftest import trending_candles as _trending_candles

from clav.clock import FakeClock
from clav.common.cache import TtlCache
from clav.data.db import make_engine, make_session_factory, session_scope
from clav.data.repositories import Repositories
from clav.data.tables import Base
from clav.domain.decision import DecisionEngine, Thresholds, Weights
from clav.domain.indicators import IndicatorService
from clav.domain.models import OrderRequest
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import TradingWindow, default_rules
from clav.domain.risk.sizing import PositionSizer
from clav.domain.social import SocialFilterParams
from clav.integrations.dryrun_broker import DryRunBroker
from clav.integrations.llm import GeminiAnalyst, GeminiBudget, GuardedLLMClient, LLMBlockedError
from clav.integrations.llm.client import LLMResult
from clav.services.analyst_gateway import AnalystGateway
from clav.services.review import TradeReviewService
from clav.services.scan_cycle import ScanCycleService
from clav.services.stop_monitor import StopMonitor

WINDOW = TradingWindow(start=time(9, 35), end=time(15, 55), timezone="America/New_York")


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "clav.db")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


class _FaultyClient:
    """Injected as the raw LLMClient behind GeminiAnalyst -- same shape as
    Epic 3's, reused here since analyze() and review() share one client in
    the real composition (epic-05 decision #3)."""

    def __init__(self, error: Exception | None = None, text: str | None = None) -> None:
        self._error = error
        self._text = text

    def generate(self, prompt: str) -> LLMResult:
        if self._error is not None:
            raise self._error
        assert self._text is not None
        return LLMResult(text=self._text, prompt_tokens=10, completion_tokens=5, model="fake")


def _services(
    session_factory, data_source, clock, analyst
) -> tuple[ScanCycleService, TradeReviewService]:
    broker = DryRunBroker(clock=clock, market_open=True)
    gateway = AnalystGateway(
        analyst=analyst,
        news_sources=[],
        social_sources=[],
        filter_params=SocialFilterParams(),
        clock=clock,
        cache=TtlCache(clock=clock, ttl_seconds=3600),
        max_age_hours=72,
        max_items_per_symbol=50,
        social_baseline_window=20,
    )
    scan_service = ScanCycleService(
        watchlist=["MSFT"],
        data_source=data_source,
        indicators=IndicatorService(),
        decision_engine=DecisionEngine(
            Weights(technical=0.5, llm=0.5, portfolio=0.0),
            Thresholds(buy=0.2, sell=-0.2),
            default_order_value=1000.0,
            clock=clock,
        ),
        risk_engine=RiskEngine(default_rules()),
        position_sizer=PositionSizer(
            risk_fraction=0.01, atr_stop_mult=2.0, take_profit_mult=2.0, default_order_value=1000.0
        ),
        stop_monitor=StopMonitor(data_source, clock=clock, quote_staleness_seconds=300),
        broker=broker,
        session_factory=session_factory,
        clock=clock,
        trading_window=WINDOW,
        max_position_value=2000.0,
        buying_power_buffer_pct=0.05,
        max_portfolio_exposure_pct=0.80,
        max_sector_allocation_pct=0.30,
        max_daily_loss_pct=0.03,
        max_drawdown_pct=0.10,
        min_avg_volume=0.0,
        earnings_blackout_days=2,
        cooldown_minutes=60,
        post_loss_cooldown_minutes=120,
        mode="dryrun",
        analyst_gateway=gateway,
    )
    review_service = TradeReviewService(
        analyst=analyst, session_factory=session_factory, clock=clock
    )
    return scan_service, review_service


def _seed_closed_trade_pending_review(session_factory, clock) -> int:
    """A closed trade with no trade_review row yet -- exactly what
    list_pending_reviews() picks up on the next pass."""
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        instrument = repos.instruments.get_or_create("AAPL")
        entry_order = repos.orders.create(
            instrument_id=instrument.id,
            decision_id=None,
            request=OrderRequest(client_order_id="clav-entry-x", symbol="AAPL", side="buy", qty=8),
            submitted_at=clock.now(),
        )
        trade = repos.trades.open_trade(
            instrument_id=instrument.id,
            entry_order_id=entry_order.id,
            entry_decision_id=None,
            qty=8,
            entry_price=100.0,
            opened_at=clock.now(),
        )
        exit_order = repos.orders.create(
            instrument_id=instrument.id,
            decision_id=None,
            request=OrderRequest(client_order_id="clav-exit-x", symbol="AAPL", side="sell", qty=8),
            submitted_at=clock.now(),
        )
        repos.trades.close_trade(
            trade.id,
            exit_order_id=exit_order.id,
            exit_price=110.0,
            closed_at=clock.now(),
            realized_pl=80.0,
            return_pct=0.10,
        )
        return trade.id


def _assert_cycle_completed(session_factory, cycle_id: str) -> None:
    with session_scope(session_factory) as session:
        repos = Repositories(session)
        cycle = repos.scan_cycles.get(cycle_id)
        assert cycle is not None
        assert cycle.status == "completed"


@pytest.mark.parametrize(
    "client",
    [
        pytest.param(_FaultyClient(error=TimeoutError("deadline exceeded")), id="timeout"),
        pytest.param(_FaultyClient(error=ConnectionError("connection reset")), id="http_error"),
        pytest.param(_FaultyClient(text="not json at all"), id="malformed_json"),
        pytest.param(
            _FaultyClient(
                text='{"why_entered": "x", "confidence_calibration": "extremely confident"}'
            ),
            id="invalid_enum",
        ),
        pytest.param(
            _FaultyClient(error=LLMBlockedError("blocked by safety filter")), id="safety_blocked"
        ),
    ],
)
def test_every_review_failure_mode_leaves_a_scan_cycle_trading_normally(
    session_factory, client
) -> None:
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({"MSFT": _trending_candles("MSFT")}, clock=clock)
    analyst = GeminiAnalyst(client)
    scan_service, review_service = _services(session_factory, data_source, clock, analyst)
    trade_id = _seed_closed_trade_pending_review(session_factory, clock)

    cycle_before = scan_service.run(trigger="manual")
    _assert_cycle_completed(session_factory, cycle_before)

    review_service.run_pass()  # must not raise regardless of failure mode

    cycle_after = scan_service.run(trigger="manual")
    _assert_cycle_completed(session_factory, cycle_after)

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        trade = repos.trades.get(trade_id)
        # a genuine failure counts an attempt but never fabricates a review
        assert trade.review_status in ("pending", "failed")
        assert repos.trade_reviews.list_for_trade(trade_id) == []


def test_budget_exhaustion_during_review_leaves_a_scan_cycle_trading_normally(
    session_factory,
) -> None:
    """The shared-budget case (epic-05 decision #3): review() defers rather
    than fails when the same GeminiBudget the scan cycle's analyze() calls
    share is exhausted -- and the scan cycle itself, also degrading to
    technical-only from the same exhausted budget, still completes."""
    clock = FakeClock(NOON_UTC)
    data_source = FakeMarketDataSource({"MSFT": _trending_candles("MSFT")}, clock=clock)
    budget = GeminiBudget(
        clock=clock,
        daily_token_budget=0,  # exhausted from the start
        daily_cost_cap_usd=0.0,
        failure_threshold=3,
        cooldown_seconds=900,
    )
    guarded = GuardedLLMClient(
        _FaultyClient(text='{"sentiment": 0.9, "conviction": 0.9, "rationale": "x"}'), budget
    )
    analyst = GeminiAnalyst(guarded)
    scan_service, review_service = _services(session_factory, data_source, clock, analyst)
    trade_id = _seed_closed_trade_pending_review(session_factory, clock)

    cycle_before = scan_service.run(trigger="manual")
    _assert_cycle_completed(session_factory, cycle_before)

    review_service.run_pass()  # must not raise; defers, doesn't count an attempt

    cycle_after = scan_service.run(trigger="manual")
    _assert_cycle_completed(session_factory, cycle_after)

    with session_scope(session_factory) as session:
        repos = Repositories(session)
        trade = repos.trades.get(trade_id)
        assert trade.review_status == "pending"
        assert trade.review_attempts == 0  # deferred, not a failed attempt
        assert repos.trade_reviews.list_for_trade(trade_id) == []
