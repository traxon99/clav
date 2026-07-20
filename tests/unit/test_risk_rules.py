from datetime import UTC, datetime, time

from clav.domain.models import PortfolioSnapshot, TradeDecision
from clav.domain.risk.rules import (
    BuyingPowerRule,
    DuplicateOrderRule,
    EmergencyStopRule,
    MaxDailyLossRule,
    MaxDrawdownRule,
    MaxPortfolioExposureRule,
    MaxPositionSizeRule,
    MaxSectorAllocationRule,
    PausedRule,
    RiskContext,
    TradingHoursRule,
    TradingWindow,
)

NOON_UTC = datetime(2025, 6, 2, 16, 0, tzinfo=UTC)  # 12:00 America/New_York (EDT, UTC-4)
DEFAULT_WINDOW = TradingWindow(start=time(9, 35), end=time(15, 55), timezone="America/New_York")


def _decision(action: str = "BUY", symbol: str = "AAPL", target_qty: int = 10) -> TradeDecision:
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
    price: float = 100.0,
    now: datetime = NOON_UTC,
    market_open: bool = True,
    max_position_value: float = 2000.0,
    buying_power_buffer_pct: float = 0.05,
    buying_power: float = 10_000.0,
    equity: float = 10_000.0,
    drawdown: float = 0.0,
    gross_exposure: float = 0.0,
    emergency_stop: bool = False,
    paused: bool = False,
    daily_start_equity: float | None = None,
    max_daily_loss_pct: float = 1.0,
    max_drawdown_pct: float = 1.0,
    max_portfolio_exposure_pct: float = 1.0,
    sector: str = "unknown",
    sector_allocation: dict[str, float] | None = None,
    max_sector_allocation_pct: float = 1.0,
    open_order_symbol_sides: frozenset[tuple[str, str]] = frozenset(),
) -> RiskContext:
    return RiskContext(
        decision=_decision(action=action),
        portfolio=PortfolioSnapshot(
            ts=now,
            cash=10_000,
            equity=equity,
            buying_power=buying_power,
            drawdown=drawdown,
            gross_exposure=gross_exposure,
            sector_allocation=sector_allocation or {},
        ),
        price=price,
        now=now,
        market_open=market_open,
        trading_window=DEFAULT_WINDOW,
        max_position_value=max_position_value,
        buying_power_buffer_pct=buying_power_buffer_pct,
        emergency_stop=emergency_stop,
        paused=paused,
        daily_start_equity=daily_start_equity,
        max_daily_loss_pct=max_daily_loss_pct,
        max_drawdown_pct=max_drawdown_pct,
        max_portfolio_exposure_pct=max_portfolio_exposure_pct,
        sector=sector,
        max_sector_allocation_pct=max_sector_allocation_pct,
        open_order_symbol_sides=open_order_symbol_sides,
    )


# --- EmergencyStopRule ---------------------------------------------------


def test_emergency_stop_vetoes_buy() -> None:
    outcome = EmergencyStopRule().apply(_ctx(action="BUY", emergency_stop=True))
    assert outcome.passed is False


def test_emergency_stop_allows_sell() -> None:
    outcome = EmergencyStopRule().apply(_ctx(action="SELL", emergency_stop=True))
    assert outcome.passed is True


def test_emergency_stop_clear_allows_buy() -> None:
    outcome = EmergencyStopRule().apply(_ctx(action="BUY", emergency_stop=False))
    assert outcome.passed is True


# --- PausedRule ------------------------------------------------------------


def test_paused_vetoes_buy() -> None:
    outcome = PausedRule().apply(_ctx(action="BUY", paused=True))
    assert outcome.passed is False


def test_paused_allows_sell() -> None:
    outcome = PausedRule().apply(_ctx(action="SELL", paused=True))
    assert outcome.passed is True


# --- TradingHoursRule -------------------------------------------------


def test_trading_hours_allows_buy_within_window() -> None:
    outcome = TradingHoursRule().apply(_ctx(action="BUY", now=NOON_UTC, market_open=True))
    assert outcome.passed is True


def test_trading_hours_vetoes_buy_when_market_closed() -> None:
    outcome = TradingHoursRule().apply(_ctx(action="BUY", market_open=False))
    assert outcome.passed is False


def test_trading_hours_vetoes_buy_outside_configured_window() -> None:
    early = datetime(2025, 6, 2, 9, 0, tzinfo=UTC)  # 05:00 America/New_York
    outcome = TradingHoursRule().apply(_ctx(action="BUY", now=early, market_open=True))
    assert outcome.passed is False


def test_trading_hours_allows_sell_even_when_market_closed() -> None:
    outcome = TradingHoursRule().apply(_ctx(action="SELL", market_open=False))
    assert outcome.passed is True


# --- MaxDailyLossRule -------------------------------------------------


def test_max_daily_loss_vetoes_buy_when_loss_at_or_above_cap() -> None:
    outcome = MaxDailyLossRule().apply(
        _ctx(action="BUY", equity=9_600.0, daily_start_equity=10_000.0, max_daily_loss_pct=0.03)
    )
    assert outcome.passed is False


def test_max_daily_loss_allows_buy_when_loss_below_cap() -> None:
    outcome = MaxDailyLossRule().apply(
        _ctx(action="BUY", equity=9_900.0, daily_start_equity=10_000.0, max_daily_loss_pct=0.03)
    )
    assert outcome.passed is True


def test_max_daily_loss_allows_sell_even_when_breached() -> None:
    outcome = MaxDailyLossRule().apply(
        _ctx(action="SELL", equity=9_000.0, daily_start_equity=10_000.0, max_daily_loss_pct=0.03)
    )
    assert outcome.passed is True


def test_max_daily_loss_passes_when_no_baseline_yet() -> None:
    outcome = MaxDailyLossRule().apply(
        _ctx(action="BUY", equity=1.0, daily_start_equity=None, max_daily_loss_pct=0.03)
    )
    assert outcome.passed is True


# --- MaxDrawdownRule ----------------------------------------------------


def test_max_drawdown_vetoes_buy_when_drawdown_at_or_above_cap() -> None:
    outcome = MaxDrawdownRule().apply(_ctx(action="BUY", drawdown=0.12, max_drawdown_pct=0.10))
    assert outcome.passed is False


def test_max_drawdown_allows_buy_when_drawdown_below_cap() -> None:
    outcome = MaxDrawdownRule().apply(_ctx(action="BUY", drawdown=0.05, max_drawdown_pct=0.10))
    assert outcome.passed is True


def test_max_drawdown_allows_sell_even_when_breached() -> None:
    outcome = MaxDrawdownRule().apply(_ctx(action="SELL", drawdown=0.50, max_drawdown_pct=0.10))
    assert outcome.passed is True


# --- MaxPortfolioExposureRule --------------------------------------------


def test_max_portfolio_exposure_caps_qty_to_remaining_budget() -> None:
    # equity=10_000, cap 30% -> budget 3000; already 2500 exposed -> 500 left @ 100/share = 5
    outcome = MaxPortfolioExposureRule().apply(
        _ctx(
            action="BUY",
            price=100.0,
            equity=10_000.0,
            gross_exposure=2_500.0,
            max_portfolio_exposure_pct=0.30,
        )
    )
    assert outcome.passed is True
    assert outcome.max_qty == 5


def test_max_portfolio_exposure_vetoes_when_already_at_or_above_cap() -> None:
    outcome = MaxPortfolioExposureRule().apply(
        _ctx(
            action="BUY",
            price=100.0,
            equity=10_000.0,
            gross_exposure=3_000.0,
            max_portfolio_exposure_pct=0.30,
        )
    )
    assert outcome.passed is False


def test_max_portfolio_exposure_ignores_sell() -> None:
    outcome = MaxPortfolioExposureRule().apply(
        _ctx(
            action="SELL",
            equity=10_000.0,
            gross_exposure=100_000.0,
            max_portfolio_exposure_pct=0.01,
        )
    )
    assert outcome.passed is True
    assert outcome.max_qty is None


# --- MaxPositionSizeRule ---------------------------------------------------


def test_max_position_size_caps_qty() -> None:
    outcome = MaxPositionSizeRule().apply(_ctx(action="BUY", price=100.0, max_position_value=550.0))
    assert outcome.passed is True
    assert outcome.max_qty == 5


def test_max_position_size_vetoes_when_price_exceeds_cap() -> None:
    outcome = MaxPositionSizeRule().apply(
        _ctx(action="BUY", price=1000.0, max_position_value=500.0)
    )
    assert outcome.passed is False


def test_max_position_size_ignores_sell() -> None:
    outcome = MaxPositionSizeRule().apply(_ctx(action="SELL", price=100000.0))
    assert outcome.passed is True
    assert outcome.max_qty is None


# --- MaxSectorAllocationRule ------------------------------------------------


def test_max_sector_allocation_caps_qty_to_remaining_sector_budget() -> None:
    # equity=10_000, cap 30% -> budget 3000; sector already has 2500 -> 500 left @ 100/share = 5
    outcome = MaxSectorAllocationRule().apply(
        _ctx(
            action="BUY",
            price=100.0,
            equity=10_000.0,
            sector="Technology",
            sector_allocation={"Technology": 2_500.0},
            max_sector_allocation_pct=0.30,
        )
    )
    assert outcome.passed is True
    assert outcome.max_qty == 5


def test_max_sector_allocation_vetoes_when_sector_already_at_or_above_cap() -> None:
    outcome = MaxSectorAllocationRule().apply(
        _ctx(
            action="BUY",
            price=100.0,
            equity=10_000.0,
            sector="Technology",
            sector_allocation={"Technology": 3_000.0},
            max_sector_allocation_pct=0.30,
        )
    )
    assert outcome.passed is False


def test_max_sector_allocation_ignores_sell() -> None:
    outcome = MaxSectorAllocationRule().apply(
        _ctx(
            action="SELL",
            equity=10_000.0,
            sector="Technology",
            sector_allocation={"Technology": 100_000.0},
            max_sector_allocation_pct=0.01,
        )
    )
    assert outcome.passed is True
    assert outcome.max_qty is None


def test_max_sector_allocation_handles_unknown_sector_without_crashing() -> None:
    # a symbol with no sector data shares the "unknown" catch-all budget
    outcome = MaxSectorAllocationRule().apply(
        _ctx(
            action="BUY",
            price=100.0,
            equity=10_000.0,
            sector="unknown",
            sector_allocation={},
            max_sector_allocation_pct=0.30,
        )
    )
    assert outcome.passed is True
    assert outcome.max_qty == 30  # full 3000 budget, nothing allocated to "unknown" yet


def test_max_sector_allocation_only_caps_the_target_sector_not_others() -> None:
    outcome = MaxSectorAllocationRule().apply(
        _ctx(
            action="BUY",
            price=100.0,
            equity=10_000.0,
            sector="Healthcare",
            sector_allocation={"Technology": 100_000.0},  # a different, maxed-out sector
            max_sector_allocation_pct=0.30,
        )
    )
    assert outcome.passed is True
    assert outcome.max_qty == 30  # Healthcare's own budget is untouched


# --- BuyingPowerRule -----------------------------------------------------


def test_buying_power_caps_qty() -> None:
    outcome = BuyingPowerRule().apply(
        _ctx(action="BUY", price=100.0, buying_power=1000.0, buying_power_buffer_pct=0.0)
    )
    assert outcome.passed is True
    assert outcome.max_qty == 10


def test_buying_power_buffer_reduces_available_qty() -> None:
    outcome = BuyingPowerRule().apply(
        _ctx(action="BUY", price=100.0, buying_power=1000.0, buying_power_buffer_pct=0.5)
    )
    assert outcome.max_qty == 5


def test_buying_power_vetoes_when_insufficient() -> None:
    outcome = BuyingPowerRule().apply(
        _ctx(action="BUY", price=1000.0, buying_power=500.0, buying_power_buffer_pct=0.0)
    )
    assert outcome.passed is False


def test_buying_power_ignores_sell() -> None:
    outcome = BuyingPowerRule().apply(_ctx(action="SELL", buying_power=0.0))
    assert outcome.passed is True


# --- DuplicateOrderRule -----------------------------------------------


def test_duplicate_order_vetoes_matching_open_order() -> None:
    outcome = DuplicateOrderRule().apply(
        _ctx(action="BUY", open_order_symbol_sides=frozenset({("AAPL", "buy")}))
    )
    assert outcome.passed is False


def test_duplicate_order_allows_when_no_open_order() -> None:
    outcome = DuplicateOrderRule().apply(_ctx(action="BUY", open_order_symbol_sides=frozenset()))
    assert outcome.passed is True


def test_duplicate_order_applies_to_sell_too() -> None:
    outcome = DuplicateOrderRule().apply(
        _ctx(action="SELL", open_order_symbol_sides=frozenset({("AAPL", "sell")}))
    )
    assert outcome.passed is False
