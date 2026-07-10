# 00 — Overview & Design Philosophy

## 1. Executive summary

CLAV (*Continuous LLM-Assisted Valuation*) is a modular, always-on trading system that
treats software engineering discipline as the primary goal and trading returns as a
secondary outcome. It runs on a Raspberry Pi 4 (2 GB RAM, Linux Mint, Python 3.11+) and
operates on a **swing-trading cadence**: it scans a watchlist of US equities on a schedule
(every 15–60 minutes plus pre/post-market passes), collects prices and news, asks Gemini
Pro to interpret qualitative information, and then runs a **deterministic decision and risk
pipeline** to produce Buy / Sell / Hold actions that are executed through Alpaca.

The system is built around a strict separation of concerns:

- **Deterministic code** owns everything that can lose money or break the law: risk limits,
  position sizing, stop-losses, trading hours, order validation, deduplication, and the
  emergency stop.
- **Gemini Pro** owns everything that requires reading and judgement: summarizing news,
  extracting catalysts, comparing sources, estimating a *confidence* number, and writing
  human-readable explanations — including a **post-trade review** that builds an evolving
  trading journal.

Every decision is logged with its full input context, the LLM's structured output, the
computed score, and which risk rules passed or blocked it. Nothing the system does is
unexplained.

## 2. Goals and non-goals

### Goals
- A **robust, modular, testable** codebase that a single engineer can maintain and extend.
- **Full auditability**: any trade can be reconstructed from the database and logs.
- **Safety first**: paper trading by default; live trading requires deliberate opt-in and
  survives multiple independent kill switches.
- **Graceful degradation**: if news, the LLM, or the internet fails, the system does not
  make reckless decisions — it holds and alerts.
- **Extensibility**: clean interfaces so brokers, data sources, models, and strategies can
  be swapped without rewrites.

### Non-goals
- Not a high-frequency / low-latency trading system.
- Not a system where the LLM autonomously decides to buy or sell.
- Not a guaranteed profit engine. Expected returns are explicitly out of scope; the metric
  of success is engineering quality and correctness.

## 3. Complete functionality list

### Market data
- Real-time and delayed quotes (last price, bid/ask, volume) via Alpaca.
- Historical daily and intraday candles (OHLCV).
- Technical indicators computed locally (SMA/EMA, RSI, MACD, ATR, Bollinger Bands, volume
  averages).
- Earnings calendar and economic-event awareness (used as **trading blackouts/flags**).

### News & qualitative data
- Financial news headlines and articles per ticker.
- Earnings releases and press releases.
- SEC filings (8-K, 10-Q, 10-K) metadata and links via EDGAR.
- Optional Reddit / X sentiment (feature-flagged; off by default).

### AI analysis (Gemini Pro)
- Summarize a bundle of news/filings per ticker.
- Extract discrete **bullish and bearish catalysts**.
- Compare multiple sources and flag disagreement.
- Detect sentiment shifts vs the previous analysis.
- Emit a **structured JSON** verdict with a `confidence` score (0–1) and rationale.
- Review completed trades and write journal entries.

### Decision & risk
- Combine technical score + LLM signal + portfolio state into a single action score.
- Apply a deterministic **risk-rule pipeline** that can only *block or shrink* a trade.
- Position sizing based on account equity, volatility (ATR), and per-position caps.
- Produce Buy / Sell / Hold only after all safety rules pass.

### Execution
- Paper trading (default) and live trading through a common `Broker` interface.
- Order validation, idempotent submission (duplicate-order prevention), retry with backoff.
- Reconciliation of fills against internal state.

### Portfolio management
- Track positions, cash, realized/unrealized P&L, exposure, drawdown, sector allocation.
- Enforce portfolio-level caps consumed by the risk engine.

### Dashboard
- Web UI showing portfolio, positions, recent trades, AI explanations, confidence scores,
  market summaries, system health, and searchable logs.
- Manual controls: pause, resume, emergency stop, force-close position (guarded).

### Persistence & observability
- SQLite database as the single source of truth.
- Structured JSON logging, rotating files, health metrics, and alerting.

## 4. The LLM/deterministic contract (advisory-signal model)

The LLM is **one weighted input**, not a decision-maker. The Decision Engine computes:

```
technical_score  ∈ [-1, +1]   # from indicators, deterministic
llm_signal       ∈ [-1, +1]   # (sentiment direction) × confidence, from Gemini JSON
portfolio_bias   ∈ [-1, +1]   # e.g. reduce toward over-exposed sectors

raw_score = w_tech * technical_score
          + w_llm  * llm_signal
          + w_port * portfolio_bias      # weights in config, sum to 1

# raw_score maps to a candidate action (BUY / SELL / HOLD) and a target size.
# THEN the deterministic risk pipeline runs. It can only veto or shrink.
final_action = risk_pipeline.apply(candidate_action, target_size, portfolio, market)
```

Guarantees enforced in code (not prompts):
- The LLM cannot bypass any risk rule. Risk rules run *after* scoring, on real numbers.
- If the LLM output is missing, malformed, low-confidence, or stale, `llm_signal` is
  clamped to `0` (neutral) — the system degrades to a purely technical + conservative mode,
  it does not guess.
- The LLM never sees API keys, account credentials, or order-submission functions. It only
  receives text and returns text/JSON.

See [06 — Safety & Risk](06-safety-and-risk.md) for the full rule pipeline and
[05 — Class Design](05-class-design.md) for how this is wired.

## 5. Reader's map

If you are implementing this, read in this order: 01 → 02 → 03 → 06 → 08 → 12, then the
rest as needed. Diagrams throughout use Mermaid so they render on GitHub and most editors.
