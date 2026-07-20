"""The risk-rule pipeline (docs/06-safety-and-risk.md §2). Epic 1 shipped 6 of
the 15 canonical rules; Epic 2 filled the rest in — Story 2.5 added the
portfolio-state circuit breakers (daily-loss, drawdown, exposure), Story 2.6
added the per-sector allocation cap, Story 2.7 added the data-integrity rules
(freshness, reconciled, liquidity), Story 2.8 added the earnings blackout, and
Story 2.9 added the symbol/post-loss cooldown. ``default_rules()`` now returns
all 15 in the exact canonical order; Story 2.10 wired every evaluated decision
to persist a ``risk_evaluation`` row (see ``ScanCycleService._process_symbol``).

Every rule can only **veto** or **cap** — never enlarge a trade. Per the
system-wide invariant in docs/06-safety-and-risk.md §2 ("Exits ... are allowed
even when new entries are frozen"), the freeze-style rules (estop/paused/
trading-hours/daily-loss/drawdown/exposure/position-size/buying-power) only
ever apply to BUY actions; SELL (exit) decisions pass them through untouched
so risk-off can never trap the system in a losing position.

This module stays pure/DB-free (no repository access, no side effects) so
every rule is testable from a plain ``RiskContext`` — the one exception,
``MaxDailyLossRule`` auto-tripping the global ``emergency_stop``
(docs/epics/epic-02-risk-and-portfolio.md, Story 2.5), is a persistence-layer
side effect and so lives in ``ScanCycleService`` instead; this rule is the
defense-in-depth veto that still fires even if that side effect is ever
skipped.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from clav.domain.models import PortfolioSnapshot, RuleOutcome, TradeDecision


@dataclass(frozen=True)
class TradingWindow:
    start: time
    end: time
    timezone: str = "America/New_York"


@dataclass(frozen=True)
class RiskContext:
    decision: TradeDecision
    portfolio: PortfolioSnapshot
    price: float
    now: datetime  # UTC-aware, from the injected Clock
    market_open: bool
    trading_window: TradingWindow
    max_position_value: float
    buying_power_buffer_pct: float
    emergency_stop: bool
    paused: bool
    daily_start_equity: float | None
    max_daily_loss_pct: float
    max_drawdown_pct: float
    max_portfolio_exposure_pct: float
    sector: str
    max_sector_allocation_pct: float
    data_stale: bool
    avg_volume: float | None
    min_avg_volume: float
    earnings_blackout: bool
    cooldown_active: bool
    open_order_symbol_sides: frozenset[tuple[str, str]] = field(default_factory=frozenset)

    @property
    def side(self) -> str:
        return "buy" if self.decision.action == "BUY" else "sell"


class RiskRule(ABC):
    name: str

    @abstractmethod
    def apply(self, ctx: RiskContext) -> RuleOutcome: ...

    def _pass(self, reason: str = "ok") -> RuleOutcome:
        return RuleOutcome(rule_name=self.name, passed=True, reason=reason)

    def _veto(self, reason: str) -> RuleOutcome:
        return RuleOutcome(rule_name=self.name, passed=False, reason=reason)

    def _cap(self, max_qty: int, reason: str) -> RuleOutcome:
        return RuleOutcome(rule_name=self.name, passed=True, max_qty=max_qty, reason=reason)


class EmergencyStopRule(RiskRule):
    name = "EmergencyStopRule"

    def apply(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.decision.action == "BUY" and ctx.emergency_stop:
            return self._veto("emergency stop is active")
        return self._pass()


class PausedRule(RiskRule):
    name = "PausedRule"

    def apply(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.decision.action == "BUY" and ctx.paused:
            return self._veto("system is paused")
        return self._pass()


class TradingHoursRule(RiskRule):
    name = "TradingHoursRule"

    def apply(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.decision.action != "BUY":
            return self._pass("exits always allowed")
        if not ctx.market_open:
            return self._veto("market is closed")
        local_now = ctx.now.astimezone(ZoneInfo(ctx.trading_window.timezone)).time()
        if not (ctx.trading_window.start <= local_now <= ctx.trading_window.end):
            return self._veto("outside configured trading window")
        return self._pass()


class DataFreshnessRule(RiskRule):
    """Rule 4: stale market data vetoes new entries. The caller resolves
    ``ctx.data_stale`` (see ``ScanCycleService._process_symbol``) from the
    latest candle's ``is_stale`` flag — set by the data adapter when a live
    fetch fails and it falls back to a cached candle set — rather than a
    wall-clock age check against the bar's own timestamp, which would be
    meaningless for the default 1Day timeframe (the latest closed daily bar
    is legitimately ~1 day old by construction). A decision must never be
    made on data the system itself doesn't trust."""

    name = "DataFreshnessRule"

    def apply(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.decision.action != "BUY":
            return self._pass("exits always allowed")
        if ctx.data_stale:
            return self._veto("quote/indicator data is stale")
        return self._pass()


class PortfolioReconciledRule(RiskRule):
    """Rule 5: a failed/skipped broker sync (``PortfolioSnapshot.reconciled``,
    Story 1.12) vetoes new entries — never size or submit against portfolio
    state the system can't currently trust."""

    name = "PortfolioReconciledRule"

    def apply(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.decision.action != "BUY":
            return self._pass("exits always allowed")
        if not ctx.portfolio.reconciled:
            return self._veto("portfolio is not reconciled with the broker")
        return self._pass()


class MaxDailyLossRule(RiskRule):
    """Rule 6: realized+unrealized loss since the last daily reset (the
    ``daily_start_equity`` baseline set by ``PortfolioManager.daily_reset``,
    Story 2.2) at or above ``max_daily_loss_pct`` vetoes new entries."""

    name = "MaxDailyLossRule"

    def apply(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.decision.action != "BUY":
            return self._pass("exits always allowed")
        if ctx.daily_start_equity is None or ctx.daily_start_equity <= 0:
            return self._pass("no daily equity baseline yet")
        daily_loss_pct = (ctx.daily_start_equity - ctx.portfolio.equity) / ctx.daily_start_equity
        if daily_loss_pct >= ctx.max_daily_loss_pct:
            return self._veto(
                f"daily loss {daily_loss_pct:.2%} at or above cap {ctx.max_daily_loss_pct:.2%}"
            )
        return self._pass()


class MaxDrawdownRule(RiskRule):
    """Rule 7: equity drawdown from the tracked peak (Story 2.2's
    ``PortfolioSnapshot.drawdown``) at or above ``max_drawdown_pct`` vetoes
    new entries."""

    name = "MaxDrawdownRule"

    def apply(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.decision.action != "BUY":
            return self._pass("exits always allowed")
        if ctx.portfolio.drawdown >= ctx.max_drawdown_pct:
            return self._veto(
                f"drawdown {ctx.portfolio.drawdown:.2%} at or above cap {ctx.max_drawdown_pct:.2%}"
            )
        return self._pass()


class MaxPortfolioExposureRule(RiskRule):
    """Rule 8: caps a BUY to whatever remains of the gross-exposure budget
    (``max_portfolio_exposure_pct`` of equity) before the trade — like
    ``MaxPositionSizeRule``, a cap that would floor to zero shares is
    reported as a veto rather than a cap(0)."""

    name = "MaxPortfolioExposureRule"

    def apply(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.decision.action != "BUY":
            return self._pass("exits always allowed")
        if ctx.price <= 0:
            return self._veto("invalid price")
        exposure_cap = ctx.max_portfolio_exposure_pct * ctx.portfolio.equity
        remaining_budget = exposure_cap - ctx.portfolio.gross_exposure
        if remaining_budget <= 0:
            return self._veto("portfolio gross exposure is already at or above cap")
        max_qty = math.floor(remaining_budget / ctx.price)
        if max_qty <= 0:
            return self._veto("remaining exposure budget is below the price of one share")
        return self._cap(max_qty, f"capped at {max_qty} shares by max_portfolio_exposure_pct")


class MaxPositionSizeRule(RiskRule):
    name = "MaxPositionSizeRule"

    def apply(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.decision.action != "BUY":
            return self._pass("exits always allowed")
        if ctx.price <= 0:
            return self._veto("invalid price")
        max_qty = math.floor(ctx.max_position_value / ctx.price)
        if max_qty <= 0:
            return self._veto("max_position_value is below the price of one share")
        return self._cap(max_qty, f"capped at {max_qty} shares by max_position_value")


class MaxSectorAllocationRule(RiskRule):
    """Rule 10: caps a BUY to whatever remains of the target sector's
    allocation budget (``max_sector_allocation_pct`` of equity) before the
    trade, reading ``PortfolioSnapshot.sector_allocation`` (Story 2.2) for the
    ``ctx.sector`` bucket the caller resolved (untagged instruments fall into
    "unknown" — see ``domain/portfolio.py`` — so this never crashes on a
    symbol with no sector data, it just shares that catch-all budget). Like
    ``MaxPositionSizeRule``, a cap that would floor to zero shares is
    reported as a veto rather than a cap(0)."""

    name = "MaxSectorAllocationRule"

    def apply(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.decision.action != "BUY":
            return self._pass("exits always allowed")
        if ctx.price <= 0:
            return self._veto("invalid price")
        sector_cap = ctx.max_sector_allocation_pct * ctx.portfolio.equity
        sector_exposure = ctx.portfolio.sector_allocation.get(ctx.sector, 0.0)
        remaining_budget = sector_cap - sector_exposure
        if remaining_budget <= 0:
            return self._veto(f"sector {ctx.sector!r} exposure is already at or above cap")
        max_qty = math.floor(remaining_budget / ctx.price)
        if max_qty <= 0:
            return self._veto("remaining sector budget is below the price of one share")
        return self._cap(max_qty, f"capped at {max_qty} shares by max_sector_allocation_pct")


class EarningsBlackoutRule(RiskRule):
    """Rule 11: an earnings event within ``earnings_blackout_days`` of now
    vetoes new entries — the technical model has no visibility into a binary
    earnings surprise, so the book stays out of the way. Missing earnings
    data for a symbol (the caller resolved ``ctx.earnings_blackout`` from
    whatever a static, config-seeded calendar knows — see
    ``ScanCycleService._check_earnings_blackout``) is fail-*open*: no known
    earnings means no blackout, a deliberate choice pending the Epic-3
    news/EDGAR-driven feed, logged by the caller rather than this pure
    rule."""

    name = "EarningsBlackoutRule"

    def apply(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.decision.action != "BUY":
            return self._pass("exits always allowed")
        if ctx.earnings_blackout:
            return self._veto("earnings event within the blackout window")
        return self._pass()


class CooldownRule(RiskRule):
    """Rule 12: an active symbol or global post-loss cooldown vetoes new
    entries. The caller resolves ``ctx.cooldown_active`` (see
    ``ScanCycleService._check_cooldown``) from two independent conditions —
    a trade in this symbol closed within ``cooldown_minutes`` (churn guard),
    or any realized loss anywhere closed within ``post_loss_cooldown_minutes``
    (revenge-trade guard) — either one is enough to freeze new entries."""

    name = "CooldownRule"

    def apply(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.decision.action != "BUY":
            return self._pass("exits always allowed")
        if ctx.cooldown_active:
            return self._veto("symbol or post-loss cooldown is active")
        return self._pass()


class BuyingPowerRule(RiskRule):
    name = "BuyingPowerRule"

    def apply(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.decision.action != "BUY":
            return self._pass("exits always allowed")
        available = ctx.portfolio.buying_power * (1 - ctx.buying_power_buffer_pct)
        if ctx.price <= 0 or available <= 0:
            return self._veto("no buying power available")
        max_qty = math.floor(available / ctx.price)
        if max_qty <= 0:
            return self._veto("insufficient buying power for even one share")
        return self._cap(max_qty, f"capped at {max_qty} shares by buying power")


class DuplicateOrderRule(RiskRule):
    """Idempotency guard: applies to both BUY and SELL — it isn't part of the
    entries-vs-exits freeze, it prevents stacking a second order for a
    symbol/side that already has one open."""

    name = "DuplicateOrderRule"

    def apply(self, ctx: RiskContext) -> RuleOutcome:
        if (ctx.decision.symbol, ctx.side) in ctx.open_order_symbol_sides:
            return self._veto("an open order already exists for this symbol/side")
        return self._pass()


class MinLiquidityRule(RiskRule):
    """Rule 15: average volume (``IndicatorSet.vol_avg_20``) below
    ``min_avg_volume`` vetoes new entries — thin names are hard to enter/exit
    without excess slippage. Missing volume history (insufficient candle
    history — see ``IndicatorSet``) fails **closed**: no data means the
    liquidity floor can't be verified, so the trade doesn't go through."""

    name = "MinLiquidityRule"

    def apply(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.decision.action != "BUY":
            return self._pass("exits always allowed")
        if ctx.avg_volume is None:
            return self._veto("no average-volume data available yet")
        if ctx.avg_volume < ctx.min_avg_volume:
            return self._veto(f"avg volume {ctx.avg_volume:.0f} below cap {ctx.min_avg_volume:.0f}")
        return self._pass()


def default_rules() -> list[RiskRule]:
    return [
        EmergencyStopRule(),
        PausedRule(),
        TradingHoursRule(),
        DataFreshnessRule(),
        PortfolioReconciledRule(),
        MaxDailyLossRule(),
        MaxDrawdownRule(),
        MaxPortfolioExposureRule(),
        MaxPositionSizeRule(),
        MaxSectorAllocationRule(),
        EarningsBlackoutRule(),
        CooldownRule(),
        BuyingPowerRule(),
        DuplicateOrderRule(),
        MinLiquidityRule(),
    ]
