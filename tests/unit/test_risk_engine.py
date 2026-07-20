from datetime import UTC, datetime, time

from hypothesis import given
from hypothesis import strategies as st

from clav.domain.models import PortfolioSnapshot, TradeDecision
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import RiskContext, TradingWindow, default_rules

NOON_UTC = datetime(2025, 6, 2, 16, 0, tzinfo=UTC)  # 12:00 America/New_York
WINDOW = TradingWindow(start=time(9, 35), end=time(15, 55), timezone="America/New_York")


def _decision(action: str, target_qty: int = 10, symbol: str = "AAPL") -> TradeDecision:
    return TradeDecision(
        cycle_id="cycle-1",
        symbol=symbol,
        action=action,  # type: ignore[arg-type]
        target_qty=target_qty,
        raw_score=0.5,
        technical_score=0.5,
        llm_signal=0.0,
        portfolio_bias=0.0,
    )


def _ctx(
    *,
    action: str = "BUY",
    target_qty: int = 10,
    price: float = 100.0,
    buying_power: float = 100_000.0,
    equity: float | None = None,
    drawdown: float = 0.0,
    gross_exposure: float = 0.0,
    max_position_value: float = 100_000.0,
    emergency_stop: bool = False,
    paused: bool = False,
    market_open: bool = True,
    now: datetime = NOON_UTC,
    daily_start_equity: float | None = None,
    max_daily_loss_pct: float = 1.0,
    max_drawdown_pct: float = 1.0,
    max_portfolio_exposure_pct: float = 1.0,
    open_order_symbol_sides: frozenset[tuple[str, str]] = frozenset(),
) -> RiskContext:
    equity = buying_power if equity is None else equity
    return RiskContext(
        decision=_decision(action, target_qty),
        portfolio=PortfolioSnapshot(
            ts=now,
            cash=buying_power,
            equity=equity,
            buying_power=buying_power,
            drawdown=drawdown,
            gross_exposure=gross_exposure,
        ),
        price=price,
        now=now,
        market_open=market_open,
        trading_window=WINDOW,
        max_position_value=max_position_value,
        buying_power_buffer_pct=0.0,
        emergency_stop=emergency_stop,
        paused=paused,
        daily_start_equity=daily_start_equity,
        max_daily_loss_pct=max_daily_loss_pct,
        max_drawdown_pct=max_drawdown_pct,
        max_portfolio_exposure_pct=max_portfolio_exposure_pct,
        open_order_symbol_sides=open_order_symbol_sides,
    )


def test_hold_action_is_never_approved() -> None:
    engine = RiskEngine(default_rules())
    result = engine.evaluate(_ctx(action="HOLD", target_qty=0))
    assert result.approved is False
    assert result.adjusted_qty == 0


def test_all_rules_pass_approves_with_full_qty() -> None:
    engine = RiskEngine(default_rules())
    result = engine.evaluate(_ctx(action="BUY", target_qty=5))
    assert result.approved is True
    assert result.adjusted_qty == 5


def test_cap_shrinks_adjusted_qty_and_still_approves() -> None:
    engine = RiskEngine(default_rules())
    result = engine.evaluate(
        _ctx(action="BUY", target_qty=10, price=100.0, max_position_value=500.0)
    )
    assert result.approved is True
    assert result.adjusted_qty == 5
    assert "MaxPositionSizeRule" not in result.blocked_by


def test_veto_blocks_regardless_of_caps() -> None:
    engine = RiskEngine(default_rules())
    result = engine.evaluate(_ctx(action="BUY", target_qty=10, emergency_stop=True))
    assert result.approved is False
    assert result.adjusted_qty == 0
    assert "EmergencyStopRule" in result.blocked_by


def test_exits_allowed_when_frozen() -> None:
    engine = RiskEngine(default_rules())
    result = engine.evaluate(
        _ctx(action="SELL", target_qty=10, emergency_stop=True, paused=True, market_open=False)
    )
    assert result.approved is True
    assert result.adjusted_qty == 10


def test_duplicate_order_blocks_even_when_otherwise_clean() -> None:
    engine = RiskEngine(default_rules())
    result = engine.evaluate(
        _ctx(action="BUY", target_qty=5, open_order_symbol_sides=frozenset({("AAPL", "buy")}))
    )
    assert result.approved is False
    assert "DuplicateOrderRule" in result.blocked_by


# --- property tests (docs/11-testing.md §1: risk invariants) --------------


@given(
    target_qty=st.integers(min_value=1, max_value=10_000),
    price=st.floats(min_value=1.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
    max_position_value=st.floats(
        min_value=1.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False
    ),
    buying_power=st.floats(
        min_value=0.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False
    ),
    emergency_stop=st.booleans(),
    paused=st.booleans(),
    market_open=st.booleans(),
)
def test_property_no_rule_ever_increases_qty(
    target_qty: int,
    price: float,
    max_position_value: float,
    buying_power: float,
    emergency_stop: bool,
    paused: bool,
    market_open: bool,
) -> None:
    engine = RiskEngine(default_rules())
    result = engine.evaluate(
        _ctx(
            action="BUY",
            target_qty=target_qty,
            price=price,
            max_position_value=max_position_value,
            buying_power=buying_power,
            emergency_stop=emergency_stop,
            paused=paused,
            market_open=market_open,
        )
    )
    assert result.adjusted_qty <= target_qty


@given(
    target_qty=st.integers(min_value=1, max_value=10_000),
    price=st.floats(min_value=1.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
    max_position_value=st.floats(
        min_value=1.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False
    ),
    buying_power=st.floats(
        min_value=0.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False
    ),
    paused=st.booleans(),
    market_open=st.booleans(),
)
def test_property_emergency_stop_blocks_every_buy(
    target_qty: int,
    price: float,
    max_position_value: float,
    buying_power: float,
    paused: bool,
    market_open: bool,
) -> None:
    engine = RiskEngine(default_rules())
    result = engine.evaluate(
        _ctx(
            action="BUY",
            target_qty=target_qty,
            price=price,
            max_position_value=max_position_value,
            buying_power=buying_power,
            emergency_stop=True,
            paused=paused,
            market_open=market_open,
        )
    )
    assert result.approved is False


@given(
    target_qty=st.integers(min_value=1, max_value=10_000),
    emergency_stop=st.booleans(),
    paused=st.booleans(),
    market_open=st.booleans(),
)
def test_property_sell_never_vetoed_by_freeze_rules(
    target_qty: int, emergency_stop: bool, paused: bool, market_open: bool
) -> None:
    engine = RiskEngine(default_rules())
    result = engine.evaluate(
        _ctx(
            action="SELL",
            target_qty=target_qty,
            emergency_stop=emergency_stop,
            paused=paused,
            market_open=market_open,
        )
    )
    assert result.approved is True
    assert result.adjusted_qty == target_qty


# --- Story 2.5: portfolio-state circuit-breaker properties -----------------


@given(
    target_qty=st.integers(min_value=1, max_value=10_000),
    price=st.floats(min_value=1.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
    daily_start_equity=st.floats(
        min_value=100.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False
    ),
    max_daily_loss_pct=st.floats(
        min_value=0.001, max_value=0.5, allow_nan=False, allow_infinity=False
    ),
    breach_margin=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
)
def test_property_max_daily_loss_blocks_every_buy_when_breached(
    target_qty: int,
    price: float,
    daily_start_equity: float,
    max_daily_loss_pct: float,
    breach_margin: float,
) -> None:
    # scale the loss into [max_daily_loss_pct, 1.0] so it's always an actual breach;
    # +epsilon guards against float round-trip noise landing exactly on the boundary.
    loss_pct = max_daily_loss_pct + breach_margin * (1.0 - max_daily_loss_pct) + 1e-9
    equity = daily_start_equity * (1 - loss_pct)

    engine = RiskEngine(default_rules())
    result = engine.evaluate(
        _ctx(
            action="BUY",
            target_qty=target_qty,
            price=price,
            buying_power=equity,
            equity=equity,
            daily_start_equity=daily_start_equity,
            max_daily_loss_pct=max_daily_loss_pct,
        )
    )
    assert result.approved is False
    assert "MaxDailyLossRule" in result.blocked_by


@given(
    target_qty=st.integers(min_value=1, max_value=10_000),
    price=st.floats(min_value=1.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
    max_drawdown_pct=st.floats(
        min_value=0.001, max_value=0.5, allow_nan=False, allow_infinity=False
    ),
    breach_margin=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
)
def test_property_max_drawdown_blocks_every_buy_when_breached(
    target_qty: int, price: float, max_drawdown_pct: float, breach_margin: float
) -> None:
    drawdown = max_drawdown_pct + breach_margin * (1.0 - max_drawdown_pct)

    engine = RiskEngine(default_rules())
    result = engine.evaluate(
        _ctx(
            action="BUY",
            target_qty=target_qty,
            price=price,
            drawdown=drawdown,
            max_drawdown_pct=max_drawdown_pct,
        )
    )
    assert result.approved is False
    assert "MaxDrawdownRule" in result.blocked_by


@given(
    target_qty=st.integers(min_value=1, max_value=10_000),
    price=st.floats(min_value=1.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
    equity=st.floats(min_value=100.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False),
    gross_exposure=st.floats(
        min_value=0.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False
    ),
    max_portfolio_exposure_pct=st.floats(
        min_value=0.01, max_value=1.0, allow_nan=False, allow_infinity=False
    ),
)
def test_property_max_portfolio_exposure_only_ever_shrinks_never_enlarges(
    target_qty: int,
    price: float,
    equity: float,
    gross_exposure: float,
    max_portfolio_exposure_pct: float,
) -> None:
    engine = RiskEngine(default_rules())
    result = engine.evaluate(
        _ctx(
            action="BUY",
            target_qty=target_qty,
            price=price,
            buying_power=equity,
            equity=equity,
            gross_exposure=gross_exposure,
            max_portfolio_exposure_pct=max_portfolio_exposure_pct,
        )
    )
    assert result.adjusted_qty <= target_qty

    exposure_cap = max_portfolio_exposure_pct * equity
    if gross_exposure >= exposure_cap:
        assert result.approved is False
