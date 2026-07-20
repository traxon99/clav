"""Typed, validated configuration for CLAV.

Non-secret settings (mode, watchlist, scan cadence, weights/thresholds, risk caps)
come from ``config/config.yaml``. Secrets (Alpaca API keys) come from environment
variables / ``.env`` and are never read from YAML, so they can never land in git via
a committed config file.
"""

from __future__ import annotations

import os
from datetime import datetime, time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from clav.common.errors import ConfigError

CONFIG_FILE_ENV_VAR = "CLAV_CONFIG_FILE"
DEFAULT_CONFIG_FILE = Path("config/config.yaml")


class AlpacaConfig(BaseModel):
    api_key: SecretStr
    api_secret: SecretStr
    base_url: str = "https://paper-api.alpaca.markets"
    data_base_url: str = "https://data.alpaca.markets"


class TradingWindowConfig(BaseModel):
    start: time = time(9, 35)
    end: time = time(15, 55)
    timezone: str = "America/New_York"

    @model_validator(mode="after")
    def _check_order(self) -> TradingWindowConfig:
        if self.start >= self.end:
            raise ValueError("trading_window.start must be before trading_window.end")
        return self


class WeightsConfig(BaseModel):
    """Weights for the raw_score formula (00-overview.md §4). Must sum to ~1.0."""

    technical: float = 1.0
    llm: float = 0.0
    portfolio: float = 0.0

    @model_validator(mode="after")
    def _check_sum(self) -> WeightsConfig:
        total = self.technical + self.llm + self.portfolio
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"weights must sum to 1.0, got {total}")
        return self


class EarningsSeedConfig(BaseModel):
    """One entry of the static, config-provided earnings calendar (Story 2.8).
    A plain config-shaped model (not ``domain.models.EarningsEvent`` — see
    ``decision.py``'s note on why config stays free of domain types); the
    composition root translates these into domain ``EarningsEvent``s to seed
    ``EarningsBlackoutRule``'s data. Full news/EDGAR-driven ingestion is
    Epic 3 — this is deliberately a static, in-memory seed."""

    symbol: str
    event_type: str = "earnings"
    scheduled_at: datetime
    confirmed: bool = False
    source: str = "config_seed"


class ThresholdsConfig(BaseModel):
    buy: float = 0.2
    sell: float = -0.2

    @model_validator(mode="after")
    def _check_order(self) -> ThresholdsConfig:
        if self.sell >= self.buy:
            raise ValueError("thresholds.sell must be lower than thresholds.buy")
        return self


class RiskConfig(BaseModel):
    """Risk caps and thresholds for the RiskEngine (docs/06-safety-and-risk.md).

    ``max_position_value`` / ``default_order_value`` are the Epic-1 flat-sizing
    caps; they are kept as the **fallback** sizing used when ATR is unavailable
    (see Story 2.3's ``PositionSizer``). The remaining fields are the Epic-2
    additions for volatility sizing, portfolio-state circuit breakers, sector
    caps, and data-integrity/earnings/cooldown rules.
    """

    max_position_value: float = Field(2000.0, gt=0)
    default_order_value: float = Field(1000.0, gt=0)
    buying_power_buffer_pct: float = Field(0.05, ge=0, lt=1)

    # Volatility-aware position sizing (Story 2.3)
    risk_fraction: float = Field(0.01, gt=0, lt=1)
    atr_stop_mult: float = Field(2.0, gt=0)
    take_profit_mult: float = Field(2.0, gt=0)

    # Portfolio-state circuit breakers (Story 2.5)
    max_daily_loss_pct: float = Field(0.03, gt=0, lt=1)
    max_drawdown_pct: float = Field(0.10, gt=0, lt=1)
    max_portfolio_exposure_pct: float = Field(0.80, gt=0, le=1)

    # Sector caps (Story 2.6)
    max_sector_allocation_pct: float = Field(0.30, gt=0, le=1)

    # Data-integrity rules (Story 2.7)
    min_avg_volume: float = Field(100_000.0, ge=0)
    quote_staleness_seconds: int = Field(300, gt=0)

    # Earnings blackout (Story 2.8)
    earnings_blackout_days: int = Field(2, ge=0)

    # Cooldowns (Story 2.9)
    cooldown_minutes: int = Field(60, ge=0)
    post_loss_cooldown_minutes: int = Field(120, ge=0)

    # Emergency-stop behavior (documented, wired in a later Epic-2 story)
    flatten_on_estop: bool = False

    @model_validator(mode="after")
    def _check_default_within_max(self) -> RiskConfig:
        if self.default_order_value > self.max_position_value:
            raise ValueError("risk.default_order_value cannot exceed risk.max_position_value")
        return self


class NewsConfig(BaseModel):
    """Free-tier news/filings source knobs (Story 3.1).

    Defaults keep the two keyless adapters (RSS + EDGAR) on and the optional paid
    NewsAPI adapter off — a fresh clone with no paid keys runs the full loop.
    """

    rss_enabled: bool = True
    rss_feed_template: str = (
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
    )
    edgar_enabled: bool = True
    edgar_filing_types: list[str] = Field(
        default_factory=lambda: ["8-K", "10-Q", "10-K", "4"]
    )
    newsapi_enabled: bool = False
    user_agent: str = "CLAV/0.1 (personal paper-trading research; contact via config)"

    @field_validator("rss_feed_template")
    @classmethod
    def _check_symbol_placeholder(cls, template: str) -> str:
        if "{symbol}" not in template:
            raise ValueError("news.rss_feed_template must contain a '{symbol}' placeholder")
        return template


class SocialConfig(BaseModel):
    """Free-tier social-sentiment knobs + Stage-1 filter thresholds (Story 3.2).

    Sources default to Reddit + StockTwits public endpoints (keyless). Thresholds
    mirror ``clav.domain.social.SocialFilterParams``; the composition root
    translates this into that domain dataclass (keeping ``domain`` config-free).
    """

    reddit_enabled: bool = True
    stocktwits_enabled: bool = True
    subreddits: list[str] = Field(
        default_factory=lambda: ["wallstreetbets", "stocks", "investing"]
    )

    # Stage-1 filter thresholds
    min_engagement_score: int = Field(5, ge=0)
    min_replies: int = Field(0, ge=0)
    min_author_reputation: float = Field(50.0, ge=0)
    max_symbols_per_post: int = Field(5, ge=1)
    near_dup_enabled: bool = True
    top_n: int = Field(5, ge=1, le=50)

    # Aggregation / anomaly guard
    anomaly_volume_multiplier: float = Field(3.0, gt=1)
    low_liquidity_volume_multiplier: float = Field(2.0, gt=1)
    min_posts_for_anomaly: int = Field(5, ge=1)

    @field_validator("subreddits")
    @classmethod
    def _normalize_subreddits(cls, subs: list[str]) -> list[str]:
        return [s.strip().lstrip("r/").strip("/") for s in subs if s.strip()]


class SourcesConfig(BaseModel):
    """Umbrella for all external content sources (news + social). Dedup/cache/
    staleness knobs are added in Story 3.3."""

    news: NewsConfig = Field(default_factory=NewsConfig)
    social: SocialConfig = Field(default_factory=SocialConfig)


class NewsApiConfig(BaseModel):
    """Optional NewsAPI secret (env/`.env` only, never YAML — like Alpaca keys).
    Absent key ⇒ the adapter is inert (returns empty), which is not an error."""

    api_key: SecretStr | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLAV_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="forbid",
    )

    mode: Literal["paper", "dryrun", "live"] = "paper"
    i_understand_live_trading: bool = False

    watchlist: list[str] = Field(min_length=1)
    scan_interval_minutes: int = Field(30, ge=1, le=1440)

    # Static symbol -> sector map (Story 2.6). Seeds ``instrument.sector`` the
    # first time an instrument is created; untagged symbols default to
    # "unknown" (see ``domain/portfolio.py``/``MaxSectorAllocationRule``). A
    # data-source-driven lookup is future work — Epic 2 keeps this static and
    # in-memory (docs/epics/epic-02-risk-and-portfolio.md, RAM discipline).
    sector_map: dict[str, str] = Field(default_factory=dict)

    # Static, config-provided earnings calendar (Story 2.8). Seeded once at
    # startup into the earnings_event table; EarningsBlackoutRule reads it
    # from there. A symbol with no entries here fails *open* (no known
    # blackout) rather than closed — deliberate, pending the Epic-3 feed.
    earnings_calendar: list[EarningsSeedConfig] = Field(default_factory=list)

    trading_window: TradingWindowConfig = Field(default_factory=TradingWindowConfig)
    weights: WeightsConfig = Field(default_factory=WeightsConfig)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    alpaca: AlpacaConfig
    newsapi: NewsApiConfig = Field(default_factory=NewsApiConfig)

    data_dir: Path = Path("./data")
    log_dir: Path = Path("./logs")

    @field_validator("watchlist")
    @classmethod
    def _normalize_watchlist(cls, symbols: list[str]) -> list[str]:
        normalized = [s.strip().upper() for s in symbols]
        if len(set(normalized)) != len(normalized):
            raise ValueError("watchlist contains duplicate symbols")
        if any(not s for s in normalized):
            raise ValueError("watchlist contains an empty symbol")
        return normalized

    @field_validator("sector_map")
    @classmethod
    def _normalize_sector_map(cls, sector_map: dict[str, str]) -> dict[str, str]:
        return {symbol.strip().upper(): sector for symbol, sector in sector_map.items()}

    @field_validator("earnings_calendar")
    @classmethod
    def _normalize_earnings_calendar(
        cls, calendar: list[EarningsSeedConfig]
    ) -> list[EarningsSeedConfig]:
        for entry in calendar:
            entry.symbol = entry.symbol.strip().upper()
        return calendar

    @model_validator(mode="after")
    def _guard_live_mode(self) -> Settings:
        if self.mode == "live":
            raise ValueError(
                "mode=live is not implemented in Epic 1 — only paper/dryrun are reachable "
                "(see docs/epics/epic-01-foundation.md). Live trading lands in Epic 6."
            )
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Priority (highest to lowest): explicit init kwargs > real env vars >
        # .env file > config.yaml > defaults. Secrets should only ever live in
        # env/.env; config.yaml should never contain them.
        yaml_file = Path(os.environ.get(CONFIG_FILE_ENV_VAR, DEFAULT_CONFIG_FILE))
        yaml_settings = YamlConfigSettingsSource(settings_cls, yaml_file=yaml_file)
        return (init_settings, env_settings, dotenv_settings, yaml_settings, file_secret_settings)

    def to_snapshot_dict(self) -> dict[str, Any]:
        """Effective config, JSON-serializable, with secrets redacted.

        Feeds the ``config_snapshot`` table (persisted per cycle) so historical
        decisions can be reproduced against the exact config that produced them.
        """
        return self.model_dump(mode="json")


def load_settings(*, env_file: str | Path = ".env") -> Settings:
    """Load and validate CLAV settings, or raise ConfigError with a readable message.

    This is the single entrypoint the rest of the app should use to obtain config —
    invalid or missing required configuration must fail loudly here, before any
    scheduler, broker, or DB connection is created.
    """
    try:
        return Settings(_env_file=env_file)
    except Exception as exc:  # pydantic.ValidationError and friends
        raise ConfigError(f"Invalid or missing configuration:\n{exc}") from exc
