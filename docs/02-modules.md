# 02 — Module Specifications

Each module below lists its **responsibility**, **inputs**, **outputs**, **key interface**,
and **failure behavior**. Modules depend on interfaces from the platform layer; concrete
adapters live in the integration layer.

---

## 1. Market Data Collector

**Responsibility:** Fetch and normalize all quantitative market data; never makes decisions.

- **Inputs:** watchlist tickers, requested timeframe, Alpaca credentials (via config).
- **Outputs:** normalized `Quote`, `Candle[]`, and derived `IndicatorSet` rows persisted to
  DB; freshness timestamps.
- **Interface:** `MarketDataSource.get_quote()`, `.get_candles()`, `.get_calendar()`.
- **Details:**
  - Pulls last price/volume and OHLCV candles (daily + configurable intraday) from Alpaca.
  - Computes indicators locally via `IndicatorService` (SMA/EMA, RSI, MACD, ATR, Bollinger,
    volume MA). Kept local so the pipeline works even if a data vendor lacks them.
  - Maintains an **earnings/economic calendar** table; upcoming earnings within N days set a
    `blackout` flag consumed by the risk engine.
- **Failure behavior:** on API error, serve last-known values marked `stale`; if staleness
  exceeds a threshold, emit `DATA_STALE` health event and the risk engine blocks new entries.

## 2. News Collector

**Responsibility:** Gather qualitative text and normalize it into `NewsItem` records. No
interpretation happens here.

- **Inputs:** watchlist tickers, source config (NewsAPI key, RSS feeds, EDGAR).
- **Outputs:** deduplicated `NewsItem[]` (source, url, headline, body, published_at,
  content_hash, tickers).
- **Interface:** `NewsSource.fetch(ticker, since) -> list[NewsItem]`.
- **Details:**
  - Adapters: `NewsApiSource`, `RssSource`, `EdgarSource` (SEC 8-K/10-Q/10-K), optional
    `RedditSource`/`XSource` behind feature flags.
  - Dedup by `content_hash` (normalized title+body) so the same story from multiple outlets
    is collapsed; keeps a `sources[]` list for the LLM to compare.
  - Rate-limit aware; caches raw responses to disk to survive restarts and avoid re-billing.
- **Failure behavior:** a failing source is skipped and logged; the cycle proceeds with
  whatever sources succeeded. Total news failure → `NEWS_UNAVAILABLE` flag → LLM step is
  skipped and `llm_signal = 0`.

## 3. AI Analysis Engine

**Responsibility:** Turn a bundle of news/filings into a **structured, bounded** verdict.
This is the only module that calls Gemini for analysis.

- **Inputs:** per-ticker `NewsItem[]`, recent price context, the *previous* analysis (for
  sentiment-shift detection).
- **Outputs:** an `AnalysisResult` persisted to DB (see schema) and a derived `llm_signal`.
- **Interface:** `Analyst.analyze(ticker, news, context) -> AnalysisResult`.
- **Contract with Gemini (enforced in code, not just prompt):**
  - Request asks for **strict JSON** matching a schema:
    ```json
    {
      "ticker": "AAPL",
      "summary": "…",
      "bullish_catalysts": ["…"],
      "bearish_catalysts": ["…"],
      "sentiment": "bullish|bearish|neutral",
      "sentiment_shift": "up|down|none",
      "confidence": 0.0,
      "source_agreement": "high|mixed|low",
      "key_risks": ["…"],
      "rationale": "…"
    }
    ```
  - Response is **validated with Pydantic**. `confidence` is clamped to `[0,1]`. Malformed →
    one retry with a repair prompt → on second failure, treated as neutral.
  - `llm_signal = sign(sentiment) * confidence`, further multiplied by a **staleness decay**
    and a **source_agreement factor** (low agreement shrinks the signal).
- **Cost/latency control:** batched per cycle, only for tickers with *new* news since last
  run (tracked by `content_hash`); results cached. A hard monthly token budget in config;
  when exceeded, analysis pauses and the system runs technical-only.
- **Failure behavior:** timeout/error/quota → neutral signal + `LLM_UNAVAILABLE` health event.

## 4. Decision Engine

**Responsibility:** Combine deterministic technical score, advisory LLM signal, and
portfolio bias into a **candidate** action and target size. It proposes; it does not execute.

- **Inputs:** `IndicatorSet`, `AnalysisResult`/`llm_signal`, `PortfolioSnapshot`, weights &
  thresholds from config.
- **Outputs:** `TradeDecision {ticker, action, target_qty, raw_score, components, reasoning}`.
- **Logic:**
  ```
  technical_score = f(indicators)          # deterministic, [-1,1]
  raw_score = w_tech*technical_score + w_llm*llm_signal + w_port*portfolio_bias
  action = BUY  if raw_score >  buy_threshold  and no position/underweight
           SELL if raw_score < sell_threshold  and holding
           HOLD otherwise
  ```
- **Determinism:** given identical inputs it always yields the same decision (pure function,
  no hidden clock reads except an injected `Clock`). This is what makes it backtestable.
- **Failure behavior:** cannot fail on external services — it only consumes already-fetched
  data. Missing LLM signal simply means `w_llm` term is 0.

## 5. Risk Engine

**Responsibility:** The mandatory safety gate. Takes a candidate decision and either
**approves, shrinks, or vetoes** it. Never *creates* or *enlarges* a trade.

- **Inputs:** candidate `TradeDecision`, `PortfolioSnapshot`, market state, risk config.
- **Outputs:** `RiskDecision {approved, adjusted_qty, blocked_by[], notes}`.
- **Rule pipeline (each rule can only tighten):** trading-hours, emergency-stop, daily-loss
  cap, max-portfolio-exposure, max-position-size, max-sector-allocation, earnings-blackout,
  data-staleness, cooldown-since-last-trade, duplicate-order, buying-power. Full details in
  [06 — Safety & Risk](06-safety-and-risk.md).
- **Position sizing** (`PositionSizer`) runs inside this stage: volatility-aware sizing
  (risk-per-trade / ATR-based stop distance), then clamped by all caps.
- **Failure behavior:** if *any* required input is unavailable, the rule **fails closed**
  (blocks the trade). Safety defaults to inaction.

## 6. Execution Engine

**Responsibility:** Validate and submit approved orders through the `Broker` interface;
reconcile fills. Idempotent and retry-safe.

- **Inputs:** approved `RiskDecision`, broker adapter, order config.
- **Outputs:** `Order` + `Fill` records; updated positions via Portfolio Manager.
- **Details:**
  - Generates a deterministic **client order id** (`hash(ticker, cycle_id, action)`), passed
    to Alpaca as `client_order_id` so a retried submission cannot double-fill.
  - Validates: qty > 0, price sanity, buying power, market open, not a duplicate open order.
  - **Retry with exponential backoff + jitter** on transient errors; permanent errors are
    logged and surfaced.
  - Reconciles: polls/streams order status until terminal (filled/canceled/rejected).
- **Modes:** `PaperBroker` (default, via Alpaca paper endpoint) and `AlpacaBroker` (live).
  A `DryRunBroker` logs intended orders without sending them (used in tests and shadow runs).
- **Failure behavior:** broker unreachable → order parked in `pending` with retry; never
  assumes success. On restart, reconciles open orders before doing anything new.

## 7. Portfolio Manager

**Responsibility:** Single source of truth for account state. Everything else reads
snapshots from it.

- **Inputs:** fills, current quotes, broker account sync.
- **Outputs:** `PortfolioSnapshot {cash, equity, positions[], unrealized_pl, realized_pl,
  exposure, drawdown, sector_allocation}`.
- **Details:**
  - Reconciles internal positions against the broker on startup and periodically (broker is
    authoritative for shares/cash; CLAV DB is authoritative for *reasoning/metadata*).
  - Computes exposure, per-sector allocation (sector map from a static reference table),
    peak-equity and drawdown for the daily-loss and max-drawdown rules.
- **Failure behavior:** if broker sync fails, marks snapshot `unreconciled`; risk engine
  blocks new entries while unreconciled.

## 8. Trade Review Service

**Responsibility:** After a position closes, ask Gemini to produce a structured post-mortem
and store it as a journal entry. Off the hot path. Full spec in
[07 — Trade Review](07-trade-review.md).

- **Inputs:** closed trade + all decisions/analysis/news captured at entry and during hold.
- **Outputs:** `TradeReview` record (why entered, supporting info, risks, whether reasoning
  was correct, misleading signals, improvement suggestions).
- **Failure behavior:** best-effort; a failed review is retried later and never blocks
  trading.

## 9. Dashboard (Web)

**Responsibility:** Observe and (guardedly) control the system.

- **Reads:** portfolio, positions, recent trades, AI explanations & confidence, market
  summaries, system health, searchable logs.
- **Controls (guarded):** pause/resume, emergency stop, toggle paper/live, force-close a
  position — each requires confirmation and is itself risk-checked and audit-logged.
- **Tech:** FastAPI backend + server-rendered HTML (HTMX) to stay light on a 2 GB Pi; no
  heavy SPA build toolchain required.

## 10. Platform services (cross-cutting)

- **ConfigService** — typed config (Pydantic Settings) from `config.yaml` + `.env`; validated
  at startup; hot-reload of *non-critical* params via the dashboard.
- **Scheduler** — APScheduler jobs: scan cycle, news pull, portfolio sync, review worker,
  health check, daily reset (peak equity, daily-loss counter).
- **LoggingService** — structured JSON logs with a `cycle_id`/`decision_id` correlation id.
- **Repositories** — thin data-access objects over SQLite; the only code that writes SQL.
- **HealthMonitor** — tracks freshness, API error rates, memory, and raises alerts / trips
  the emergency stop on critical conditions.
