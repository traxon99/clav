"""Composition root & CLI entrypoint for clav-core (docs/05-class-design.md §4).

Assembles the entire scan-cycle pipeline from config in one place — nothing
deep in the tree constructs its own dependencies, which is what keeps CLAV
testable and swappable (paper/dryrun via ``broker_factory``).
"""

from __future__ import annotations

import time

import click
from sqlalchemy.orm import Session, sessionmaker

from clav.clock import Clock, SystemClock
from clav.common.cache import TtlCache
from clav.common.errors import ConfigError
from clav.common.logging import configure_logging, get_logger
from clav.config import Settings, load_settings
from clav.data.db import make_engine, make_session_factory
from clav.domain.decision import DecisionEngine, Thresholds, Weights
from clav.domain.indicators import IndicatorService
from clav.domain.models import EarningsEvent
from clav.domain.risk.engine import RiskEngine
from clav.domain.risk.rules import TradingWindow, default_rules
from clav.domain.risk.sizing import PositionSizer
from clav.domain.social import SocialFilterParams
from clav.integrations.alerting import SmtpAlertChannel, WebhookAlertChannel
from clav.integrations.alpaca_data import AlpacaDataAdapter
from clav.integrations.broker_factory import broker_factory
from clav.integrations.llm import (
    AnalysisCapture,
    GeminiAnalyst,
    GeminiBudget,
    GeminiRestClient,
    GuardedLLMClient,
)
from clav.integrations.news import EdgarNewsSource, NewsApiSource, RSSNewsSource
from clav.integrations.social import RedditSource, StockTwitsSource
from clav.integrations.system_metrics import PsutilSystemMetricsCollector
from clav.interfaces.alerting import AlertChannel
from clav.interfaces.analyst import Analyst
from clav.interfaces.news import NewsSource
from clav.interfaces.social import SocialSource
from clav.services.alerting import Alerter
from clav.services.analyst_gateway import AnalystGateway
from clav.services.decision_journal import ApprovalPolicy
from clav.services.health_monitor import HealthMonitor
from clav.services.prompt_store import PromptVersionStore
from clav.services.runtime_config import RuntimeConfigStore
from clav.services.scan_cycle import ScanCycleService
from clav.services.scheduler import Scheduler
from clav.services.stop_monitor import StopMonitor

_logger = get_logger(__name__)


def _build_news_sources(cfg: Settings, *, clock: Clock) -> list[NewsSource]:
    """Free, keyless sources are on by default (RSS + EDGAR); NewsAPI only
    activates when a key is configured (epic decision #5 — paid sources are
    opt-in, never on the critical path)."""
    sources: list[NewsSource] = []
    if cfg.sources.news.rss_enabled:
        sources.append(RSSNewsSource(clock=clock, feed_template=cfg.sources.news.rss_feed_template))
    if cfg.sources.news.edgar_enabled:
        sources.append(
            EdgarNewsSource(clock=clock, filing_types=tuple(cfg.sources.news.edgar_filing_types))
        )
    if cfg.sources.news.newsapi_enabled:
        api_key = cfg.newsapi.api_key.get_secret_value() if cfg.newsapi.api_key else None
        sources.append(NewsApiSource(clock=clock, api_key=api_key))
    return sources


def _build_social_sources(cfg: Settings, *, clock: Clock) -> list[SocialSource]:
    """Free-tier public endpoints only — no key/app/approval required (Story
    3.2). Both degrade to an empty digest on failure; neither is required for
    the loop to run."""
    sources: list[SocialSource] = []
    if cfg.sources.social.reddit_enabled:
        sources.append(RedditSource(clock=clock, subreddits=tuple(cfg.sources.social.subreddits)))
    if cfg.sources.social.stocktwits_enabled:
        sources.append(StockTwitsSource(clock=clock))
    return sources


def _build_analyst(
    cfg: Settings, *, session_factory: sessionmaker[Session], clock: Clock
) -> tuple[Analyst, GeminiBudget, AnalysisCapture]:
    """Gemini is a proposer behind the risk gate (epic decision #1). With no
    API key configured, GeminiRestClient.generate() itself raises and
    GeminiAnalyst degrades to a neutral signal — the loop runs technical-only
    on a fresh clone with no paid keys (epic-level DoD).

    The ``AnalysisCapture`` is installed as the analyst's provenance sink and
    handed back so the gateway can drain + persist the redacted request/response
    each cycle (Story 3.12 provenance closure)."""
    prompt_store = PromptVersionStore(session_factory, clock=clock)
    prompt_store.seed_default(persona=cfg.llm.default_persona)

    api_key = cfg.llm.api_key.get_secret_value() if cfg.llm.api_key else None
    rest_client = GeminiRestClient(
        api_key=api_key,
        model=cfg.llm.model,
        timeout=cfg.llm.timeout_seconds,
        max_output_tokens=cfg.llm.max_output_tokens,
    )
    budget = GeminiBudget(
        clock=clock,
        daily_token_budget=cfg.llm.daily_token_budget,
        daily_cost_cap_usd=cfg.llm.daily_cost_cap_usd,
        failure_threshold=cfg.llm.breaker_failure_threshold,
        cooldown_seconds=cfg.llm.breaker_cooldown_seconds,
        cost_per_1k_prompt_tokens_usd=cfg.llm.cost_per_1k_prompt_tokens_usd,
        cost_per_1k_completion_tokens_usd=cfg.llm.cost_per_1k_completion_tokens_usd,
    )
    guarded_client = GuardedLLMClient(rest_client, budget)
    capture = AnalysisCapture()
    analyst = GeminiAnalyst(
        guarded_client,
        persona_provider=prompt_store.get_active,
        provenance_sink=capture.record,
    )
    return analyst, budget, capture


def build_analyst_gateway(
    cfg: Settings, *, session_factory: sessionmaker[Session], clock: Clock
) -> AnalystGateway:
    analyst, budget, capture = _build_analyst(cfg, session_factory=session_factory, clock=clock)
    filter_params = SocialFilterParams(
        min_engagement_score=cfg.sources.social.min_engagement_score,
        min_replies=cfg.sources.social.min_replies,
        min_author_reputation=cfg.sources.social.min_author_reputation,
        max_symbols_per_post=cfg.sources.social.max_symbols_per_post,
        near_dup_enabled=cfg.sources.social.near_dup_enabled,
        top_n=cfg.sources.social.top_n,
        anomaly_volume_multiplier=cfg.sources.social.anomaly_volume_multiplier,
        low_liquidity_volume_multiplier=cfg.sources.social.low_liquidity_volume_multiplier,
        min_posts_for_anomaly=cfg.sources.social.min_posts_for_anomaly,
    )
    return AnalystGateway(
        analyst=analyst,
        news_sources=_build_news_sources(cfg, clock=clock),
        social_sources=_build_social_sources(cfg, clock=clock),
        filter_params=filter_params,
        clock=clock,
        cache=TtlCache(clock=clock, ttl_seconds=cfg.sources.cache_ttl_seconds),
        max_age_hours=cfg.sources.max_age_hours,
        max_items_per_symbol=cfg.sources.max_items_per_symbol,
        social_baseline_window=cfg.sources.social_baseline_window,
        reset_daily_hook=budget.reset_daily,
        budget=budget,
        analysis_capture=capture,
    )


def build_alerter(cfg: Settings, *, clock: Clock) -> Alerter:
    """Both channels are **off by default** (epic decision #4 / Story 4.3):
    absent/disabled config means the ``Alerter`` simply has no channels to
    fan out to — every alert still logs and is persisted as a health_event
    by its caller, it just never sends anywhere. Secrets come from env/.env
    only (``SecretStr``), never ``config.yaml``."""
    channels: list[AlertChannel] = []
    if cfg.alerts.smtp.enabled:
        channels.append(
            SmtpAlertChannel(
                host=cfg.alerts.smtp.host,
                port=cfg.alerts.smtp.port,
                use_tls=cfg.alerts.smtp.use_tls,
                username=cfg.alerts.smtp.username,
                password=(
                    cfg.alerts.smtp.password.get_secret_value()
                    if cfg.alerts.smtp.password
                    else None
                ),
                from_addr=cfg.alerts.smtp.from_addr,
                to_addr=cfg.alerts.smtp.to_addr,
            )
        )
    if cfg.alerts.webhook.enabled:
        channels.append(
            WebhookAlertChannel(
                url=cfg.alerts.webhook.url,
                token=(
                    cfg.alerts.webhook.token.get_secret_value()
                    if cfg.alerts.webhook.token
                    else None
                ),
            )
        )
    return Alerter(
        clock=clock,
        channels=channels,
        critical_dedup_minutes=cfg.alerts.critical_dedup_minutes,
        digest_interval_minutes=cfg.alerts.digest_interval_minutes,
    )


def build_scan_cycle_service(cfg: Settings, *, clock: Clock | None = None) -> ScanCycleService:
    clock = clock or SystemClock()

    engine = make_engine(cfg.data_dir / "clav.db")
    session_factory = make_session_factory(engine)

    api_key = cfg.alpaca.api_key.get_secret_value()
    api_secret = cfg.alpaca.api_secret.get_secret_value()

    broker = broker_factory(
        cfg.mode, clock=clock, alpaca_api_key=api_key, alpaca_api_secret=api_secret
    )
    data_source = AlpacaDataAdapter(api_key, api_secret, clock=clock)

    decision_engine = DecisionEngine(
        Weights(
            technical=cfg.weights.technical, llm=cfg.weights.llm, portfolio=cfg.weights.portfolio
        ),
        Thresholds(buy=cfg.thresholds.buy, sell=cfg.thresholds.sell),
        default_order_value=cfg.risk.default_order_value,
        clock=clock,
    )
    position_sizer = PositionSizer(
        risk_fraction=cfg.risk.risk_fraction,
        atr_stop_mult=cfg.risk.atr_stop_mult,
        take_profit_mult=cfg.risk.take_profit_mult,
        default_order_value=cfg.risk.default_order_value,
    )
    stop_monitor = StopMonitor(
        data_source, clock=clock, quote_staleness_seconds=cfg.risk.quote_staleness_seconds
    )
    earnings_calendar = [
        EarningsEvent(
            symbol=entry.symbol,
            event_type=entry.event_type,
            scheduled_at=entry.scheduled_at,
            confirmed=entry.confirmed,
            source=entry.source,
        )
        for entry in cfg.earnings_calendar
    ]

    analyst_gateway = build_analyst_gateway(cfg, session_factory=session_factory, clock=clock)
    approval_policy = ApprovalPolicy(
        mode=cfg.approval.mode,
        ttl_minutes=cfg.approval.ttl_minutes,
        per_symbol=dict(cfg.approval.per_symbol),
    )
    alerter = build_alerter(cfg, clock=clock)
    health_monitor = HealthMonitor(
        clock=clock,
        system_metrics=PsutilSystemMetricsCollector(),
        db_path=cfg.data_dir / "clav.db",
        thresholds=cfg.observability,
        alerter=alerter,
    )

    def alert_hook(condition: str, message: str) -> None:
        # Story 4.3: wire the previously-unwired alert_hook seam through the
        # same Alerter — every ad-hoc execution/daily-loss alert is CRITICAL
        # by the existing convention at those call sites.
        alerter.notify(condition, "critical", message)

    return ScanCycleService(
        watchlist=cfg.watchlist,
        data_source=data_source,
        indicators=IndicatorService(),
        decision_engine=decision_engine,
        risk_engine=RiskEngine(default_rules()),
        position_sizer=position_sizer,
        stop_monitor=stop_monitor,
        broker=broker,
        session_factory=session_factory,
        clock=clock,
        alert_hook=alert_hook,
        trading_window=TradingWindow(
            start=cfg.trading_window.start,
            end=cfg.trading_window.end,
            timezone=cfg.trading_window.timezone,
        ),
        max_position_value=cfg.risk.max_position_value,
        buying_power_buffer_pct=cfg.risk.buying_power_buffer_pct,
        max_portfolio_exposure_pct=cfg.risk.max_portfolio_exposure_pct,
        max_sector_allocation_pct=cfg.risk.max_sector_allocation_pct,
        max_daily_loss_pct=cfg.risk.max_daily_loss_pct,
        max_drawdown_pct=cfg.risk.max_drawdown_pct,
        min_avg_volume=cfg.risk.min_avg_volume,
        earnings_blackout_days=cfg.risk.earnings_blackout_days,
        cooldown_minutes=cfg.risk.cooldown_minutes,
        post_loss_cooldown_minutes=cfg.risk.post_loss_cooldown_minutes,
        mode=cfg.mode,
        sector_map=cfg.sector_map,
        earnings_calendar=earnings_calendar,
        analyst_gateway=analyst_gateway,
        approval_policy=approval_policy,
        runtime_config=RuntimeConfigStore(),
        health_monitor=health_monitor,
    )


def run_core() -> None:
    try:
        cfg = load_settings()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error:\n{exc}") from None

    configure_logging(log_dir=cfg.log_dir)
    _logger.info("clav_core_starting", mode=cfg.mode, watchlist=cfg.watchlist)

    service = build_scan_cycle_service(cfg)
    scheduler = Scheduler(service, scan_interval_minutes=cfg.scan_interval_minutes)
    scheduler.start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        _logger.info("clav_core_shutting_down")
        scheduler.shutdown()


@click.group()
def cli() -> None:
    """CLAV core entrypoints."""


@cli.command()
def core() -> None:
    """Run the clav-core trading daemon (systemd ExecStart target)."""
    run_core()


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
