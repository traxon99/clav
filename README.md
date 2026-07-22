# CLAV — Continuous LLM-Assisted Valuation

CLAV (Clavicular) is an autonomous, AI-assisted **swing-trading** system designed to run 24/7 on a
Raspberry Pi 4 (2 GB RAM). It monitors a watchlist of US equities, collects market data
and news, uses **Gemini Pro as an analyst** (never as the sole decision-maker), and lets
**deterministic software own all risk and execution decisions**.

Paper trading is the default. Live trading is opt-in and gated behind explicit config,
risk checks, and an emergency stop.

## Key architectural decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Broker / data | **Alpaca** (US equities) | Free paper + live API, `alpaca-py` SDK, built-in market data |
| Cadence | **Swing / daily** (15–60 min scans) | Fits a 2 GB Pi; LLM latency & cost make intraday impractical |
| LLM role | **Advisory signal** | LLM emits structured sentiment/catalysts/confidence; Decision Engine combines via tunable weights behind hard risk gates |
| Database | **SQLite (WAL mode)** | Single-node, low-RAM, zero-admin; Postgres is a documented migration path |
| Language | **Python 3.11+** | Ecosystem fit; async where it helps I/O |
| Cost | **Free-tier only** (for now) | Every data source & host must have a usable free tier — no paid keys/hosting required to run end-to-end (news = RSS + SEC EDGAR; social = Reddit [public reads, non-commercial] + StockTwits [public reads]; Gemini cost-capped, operator on a 1-yr complimentary Gemini Pro grant). Paid sources sit behind interfaces as opt-in upgrades, off by default |

## Design philosophy in one sentence

> Deterministic code decides *whether it is safe to trade and how much*; the LLM only
> decides *what the news means* — and every number it produces is one weighted input into
> an auditable scoring function.

## Documentation index

| # | Document | Deliverables covered |
|---|----------|----------------------|
| 00 | [Overview & Philosophy](docs/00-overview.md) | Executive summary, full functionality list, LLM-vs-deterministic split |
| 01 | [System Architecture](docs/01-architecture.md) | Architecture diagram, module diagram, data-flow diagram |
| 02 | [Module Specifications](docs/02-modules.md) | All 10 modules in detail (responsibilities, I/O, interfaces) |
| 03 | [Database Design](docs/03-database.md) | SQLite vs Postgres, schema, tables, relationships (ER diagram) |
| 04 | [External Integrations](docs/04-integrations.md) | Alpaca / Gemini / news API interactions, request-response flow |
| 05 | [Class Design & Patterns](docs/05-class-design.md) | Class diagram, suggested design patterns |
| 06 | [Safety & Risk Engine](docs/06-safety-and-risk.md) | All safeguards, risk-rule pipeline |
| 07 | [Trade Review System](docs/07-trade-review.md) | Post-trade Gemini journal |
| 08 | [Project Structure](docs/08-project-structure.md) | Folder structure, package & library recommendations |
| 09 | [Deployment](docs/09-deployment.md) | Raspberry Pi deployment, systemd, resource budget |
| 10 | [Observability & Logging](docs/10-observability.md) | Logging strategy, metrics, alerting |
| 11 | [Testing Strategy](docs/11-testing.md) | Unit / integration / backtest / chaos testing |
| 12 | [Development Roadmap](docs/12-roadmap.md) | Phased delivery plan |
| 13 | [Flows & Lifecycles](docs/13-flows.md) | Example request/response flow, full trade lifecycle (sequence diagrams) |
| 14 | [Future Expansion](docs/14-future-expansion.md) | Multi-agent, multi-broker, crypto, RL, local LLM, distributed |

## Delivery

| Epic | Document | Scope |
|------|----------|-------|
| 1 | [Foundation & First Autonomous Paper Trade](docs/epics/epic-01-foundation.md) | Skeleton + technical-only end-to-end paper loop with minimal guardrails (Roadmap Phases 0–1) |
| 2 | [Full Risk Engine, Volatility Sizing & Portfolio Accounting](docs/epics/epic-02-risk-and-portfolio.md) | Full 15-rule risk pipeline, ATR sizing + stops, real exposure/drawdown/sector accounting, persisted risk evaluations (Roadmap Phase 2) |
| 3 | [Gemini Analyst, News, Social Sentiment & Human-Steerable Trading](docs/epics/epic-03-gemini-and-control.md) | Free-tier news (RSS/EDGAR) + social-sentiment (Reddit/StockTwits, deterministically de-spammed) feeding a `GeminiAnalyst` (strict-JSON, neutral fallback, cost breaker) that proposes trades behind the risk gate and **executes them autonomously**, with a **decision journal** + minimal web UI to review the rationale and tune it (weights/risk/prompt/watchlist); per-trade approval is an optional off-by-default mode (Roadmap Phase 3) |
| 4 | [Observability Dashboard, Metrics & Alerting](docs/epics/epic-04-dashboard-and-observability.md) | `HealthMonitor` + `health_event`, rich `/health` + Prometheus `/metrics`, pluggable off-by-default alerting (email/webhook), an HTMX dashboard (equity/drawdown charts, AI-explanation + confidence over the full provenance chain, system-health tiles, daily-loss gauge, searchable audit browser), and per-cycle `config_snapshot` reproducibility (Roadmap Phase 4) |
| 5 | [Trade Review Journal & Score Calibration](docs/epics/epic-05-trade-review-and-calibration.md) | `Analyst.review()` + `TradeReviewService`, a durable `trade_review` journal for every closed trade (why entered, what worked, misleading signals, hindsight, improvements), a `/reviews` dashboard view, and a confidence-calibration/tag aggregation panel on `/calibration` (Roadmap Phase 5) |
| 6 | [Live-Trading Gate & Soak](docs/epics/epic-06-live-trading-and-soak.md) | A live `AlpacaBroker` behind a fail-closed **two-key gate** (`mode: live` + `i_understand_live_trading`), `flatten-on-estop`, a persistent **LIVE** banner + mode surfacing, a capital-capped pilot profile, and soak tooling + go-live checklist — broker + visibility only, no new risk rules or analyst changes (Roadmap Phase 6, **planned**) |

## Status

Epics 1–5 are implemented; the system paper-trades a watchlist end-to-end with a full
risk engine, a Gemini analyst behind the risk gate, an observability dashboard, and a
trade-review journal. Live trading (Epic 6) is not yet wired. Each epic doc has the
detail; the runbook below has the operational commands.

- **Epic 1 — [Foundation](docs/epics/epic-01-foundation.md).** An always-on skeleton that
  scans a watchlist, makes technical-only Buy/Sell/Hold decisions, executes them idempotently
  against Alpaca **paper**, tracks the portfolio, and persists a full provenance trail, with
  minimal guardrails and an emergency stop.
- **Epic 2 — [Risk & Portfolio](docs/epics/epic-02-risk-and-portfolio.md).** All 15 risk
  rules run in order, ATR-based sizing with stop-loss/take-profit, a stop-monitor that exits
  independently of the decision path, real exposure/drawdown/sector accounting, and a
  persisted `risk_evaluation` per decision. Still `llm_signal = 0`.
- **Epic 3 — [Gemini & Control](docs/epics/epic-03-gemini-and-control.md).** Free-tier news
  (RSS/EDGAR) and social sentiment (Reddit/StockTwits, deterministically de-spammed) feed a
  `GeminiAnalyst` that **proposes** trades behind the Epic-2 risk gate; any Gemini failure
  degrades to a neutral technical-only signal (proven by a prompt-injection chaos suite).
  Trades execute autonomously once they pass risk, land in a reviewable **decision journal**,
  and a minimal `clav-web` UI lets the operator supervise and tune (prompt/weights/risk/
  watchlist, e-stop); per-symbol approval is an optional off-by-default mode. All free-tier.
  See [Runbook — Epic 3](#epic-3-runbook-gemini-newssocial-and-the-control-uiapi).
- **Epic 4 — [Dashboard & Observability](docs/epics/epic-04-dashboard-and-observability.md).**
  A `HealthMonitor` writes durable `health_event` rows backing a rich `/health` and a
  Prometheus `/metrics`; a DB-free `Alerter` sends severity-gated email/webhook alerts (off by
  default). `clav-web` gains five inline-SVG/HTMX pages (health tiles + daily-loss gauge,
  Portfolio, Explanations, a searchable Audit browser, a descriptive Calibration view), and a
  per-cycle `config_snapshot` makes any decision reproducible.
  See [Runbook — Epic 4](#epic-4-runbook-dashboard-alerting-and-observability).
- **Epic 5 — [Trade Review & Calibration](docs/epics/epic-05-trade-review-and-calibration.md).**
  A `TradeReviewService` runs as its own scheduled pass, sharing Epic 3's `GeminiAnalyst`/
  `GeminiBudget` (one daily allowance, not two); a genuine failure retries with backoff and
  eventually gives up, a budget/breaker hit defers instead. Every closed trade gets a
  structured post-mortem in a `/reviews` journal (why entered, what worked, misleading
  signals, hindsight), and `/calibration` gains a second panel checking whether the model's
  own stated confidence actually tracked outcome. An operator can force a re-review from the
  dashboard or the API. See [Runbook — Epic 5](#epic-5-runbook-trade-review-and-calibration).

## Getting started (development)

```bash
# uv manages the Python version and virtualenv (see pyproject.toml)
uv sync --all-groups

cp config/config.example.yaml config/config.yaml   # edit watchlist/schedule
cp .env.example .env                                 # fill in real Alpaca *paper* keys

uv run alembic upgrade head
uv run pytest                                         # full test suite (offline, no network)
```

## Runbook

### Start / stop

- **Dev, foreground:** `uv run clav core` runs the scheduler in the current terminal
  (`Ctrl-C` to stop; it reconciles on the next start).
- **Pi, systemd** (after `sudo ./deploy/install.sh`, see [09 — Deployment](docs/09-deployment.md)):
  ```bash
  sudo systemctl start clav-core     # start
  sudo systemctl stop clav-core      # stop
  sudo systemctl restart clav-core   # restart (reconciles before trading resumes)
  sudo systemctl status clav-core    # is it running?
  ```

### Trip / clear the emergency stop

The `clav-ctl` CLI writes to the `system_control` table that every scan cycle polls
(Story 1.10). It works whether or not `clav-core` itself is currently healthy.

```bash
uv run clav-ctl status         # show emergency_stop / paused
uv run clav-ctl estop-set      # trip it: vetoes all new BUY entries, exits still allowed
uv run clav-ctl estop-clear    # clear it
uv run clav-ctl pause          # pause: same effect as estop, separate flag
uv run clav-ctl resume
```

On the Pi, run the same commands as the `clav` user against the installed venv:
`sudo -u clav /opt/clav/.venv/bin/clav-ctl status`.

### Read the logs

Structured JSON, one line per event, correlated by `cycle_id`; secrets are redacted.

```bash
# Dev: written under log_dir from config.yaml (./logs by default)
tail -f logs/clav.log | jq .

# Pi: journald captures stdout under systemd
journalctl -u clav-core -f
journalctl -u clav-core -f | jq 'select(.cycle_id == "<cycle-id-from-a-log-line>")'
```

### Risk config & reading a `risk_evaluation` row (Epic 2)

**The risk config knobs** (`config.yaml` `risk:` section — see `config.example.yaml` for a fully
commented template; full definitions in [06 — Safety & Risk](docs/06-safety-and-risk.md) and
[epic-02](docs/epics/epic-02-risk-and-portfolio.md)):

| Knob | Rule it feeds | Meaning |
|------|----------------|---------|
| `max_position_value`, `default_order_value` | `MaxPositionSizeRule`, `PositionSizer` fallback | Per-name USD cap; flat-sizing fallback when ATR is unavailable |
| `buying_power_buffer_pct` | `BuyingPowerRule` | Fraction of buying power held back as a safety margin |
| `risk_fraction`, `atr_stop_mult`, `take_profit_mult` | `PositionSizer` | ATR-based sizing: risk-per-trade, stop distance, take-profit distance |
| `max_daily_loss_pct` | `MaxDailyLossRule` | Daily loss ≥ this auto-trips `emergency_stop` |
| `max_drawdown_pct` | `MaxDrawdownRule` | Equity drawdown from peak ≥ this vetoes new entries |
| `max_portfolio_exposure_pct` | `MaxPortfolioExposureRule` | Gross exposure cap as a fraction of equity |
| `max_sector_allocation_pct` | `MaxSectorAllocationRule` | Per-sector exposure cap as a fraction of equity |
| `min_avg_volume` | `MinLiquidityRule` | 20-day avg volume floor; missing data vetoes (fails closed) |
| `quote_staleness_seconds` | `StopMonitor` | Live quotes older than this (or flagged stale) skip a stop-monitor check |
| `earnings_blackout_days` | `EarningsBlackoutRule` | Veto window around a known earnings event; **no** known event is fail-*open* (allowed), logged |
| `cooldown_minutes`, `post_loss_cooldown_minutes` | `CooldownRule` | Per-symbol re-entry cooldown after a close; global cooldown after any realized loss |
| `flatten_on_estop` | `StopMonitor.flatten()` | When `true` and `emergency_stop` is tripped (manual or auto), force-closes every open position through the normal exit path instead of just freezing new entries; default `false` (freeze-only, unchanged) |

`sector_map` (top-level, not under `risk:`) seeds `instrument.sector`; `earnings_calendar`
(top-level) is the static, config-provided earnings seed — see the commented examples in
`config.example.yaml`.

**Going live (Epic 6):** `config/config.pilot.example.yaml` is a committed, fully-commented
capital-capped profile for a first live pilot — the same risk knobs above, just set far tighter
(small `max_position_value`, low `max_daily_loss_pct`/`max_drawdown_pct`, a two-symbol watchlist,
`flatten_on_estop: true`). It enforces nothing new; every cap is the existing risk pipeline, sized
down. Copy it to `config.yaml` only after the go-live checklist (Story 6.6) is signed off.

**Reading a `risk_evaluation` row** — every decision the risk engine sees (BUY, SELL, or HOLD,
including a candidate BUY the position sizer shrank to zero) gets one, linked to its `decision`
row:

```
sqlite3 data/clav.db "SELECT approved, adjusted_qty, blocked_by, notes FROM risk_evaluation
  WHERE decision_id = (SELECT id FROM decision WHERE scan_cycle_id='<cycle_id>'
  AND instrument_id=(SELECT id FROM instrument WHERE symbol='AAPL'));"
```

`notes` is a JSON object keyed by rule name, in the exact order the 15 rules ran
(`docs/06-safety-and-risk.md` §2), each with `{"passed": bool, "max_qty": int|null, "reason": str}`.
`blocked_by` lists every rule that vetoed (there can be more than one); `adjusted_qty` is the
`min()` across every rule's cap. A **cap** looks like `approved=true`, `blocked_by=[]`, and the
capping rule's entry in `notes` has a `max_qty` matching `adjusted_qty`:

```json
{"approved": true, "adjusted_qty": 9, "blocked_by": [],
 "notes": {"...": "...", "MaxPositionSizeRule": {"passed": true, "max_qty": 9, "reason": "capped at 9 shares by max_position_value"}, "...": "..."}}
```

A **veto** looks like `approved=false`, `adjusted_qty=0`, and the vetoing rule(s) in `blocked_by`:

```json
{"approved": false, "adjusted_qty": 0, "blocked_by": ["EarningsBlackoutRule"],
 "notes": {"...": "...", "EarningsBlackoutRule": {"passed": false, "max_qty": null, "reason": "earnings event within the blackout window"}, "...": "..."}}
```

The same shape shows up in the logs on every symbol, every cycle, without needing the DB —
look for the `risk_evaluated` event:

```bash
journalctl -u clav-core -f | jq 'select(.event == "risk_evaluated")'
# {"event": "risk_evaluated", "symbol": "AAPL", "approved": true, "adjusted_qty": 9, "blocked_by": [], "cycle_id": "...", ...}
```

A daily-loss breach additionally logs `daily_loss_auto_estop_tripped` (or
`daily_loss_alert_no_hook_configured` if no `alert_hook` is wired) at `critical` — that's the one
risk event that also durably flips `system_control.emergency_stop`, not just this cycle's decision.

### Epic 3 runbook (Gemini, news/social, and the control UI/API)

Everything in this section has a usable **free tier**; a fresh clone with no paid keys
configured still runs the full loop — Gemini simply degrades to a neutral, technical-only
signal (`llm_signal = 0`) until you add a key. All new keys/knobs live in the `sources:`,
`llm:`, `approval:`, and `web:` blocks of `config.example.yaml` (fully commented) plus the
optional secrets in `.env.example`.

#### Configuring the free news/social sources

- **News** (`sources.news`) — `rss_enabled`/`edgar_enabled` (both `true` by default, keyless):
  RSS pulls a per-symbol headline feed (`rss_feed_template`, `{symbol}` placeholder); EDGAR pulls
  SEC filings (`edgar_filing_types`, default `8-K`/`10-Q`/`10-K`/`Form-4`). `newsapi_enabled` is
  `false` by default — a **paid** product; set it `true` **and** `CLAV_NEWSAPI__API_KEY` in
  `.env` to opt in, otherwise it's simply inert.
- **Social** (`sources.social`) — `reddit_enabled`/`stocktwits_enabled` (both `true` by default,
  keyless public endpoints; `subreddits` is configurable). Both degrade to an empty digest on
  failure/rate-limit and are never required for the loop to run.
- **Dedup/cache/retention** (`sources.cache_ttl_seconds`, `max_age_hours`,
  `max_items_per_symbol`, `social_baseline_window`) — how long before re-fetching, how stale is
  too stale for analysis, how many rows to keep per symbol, and the rolling window the social
  anomaly guard compares a new spike against.

#### Configuring Gemini

1. Get a key at <https://aistudio.google.com/apikey> (the free tier is usable on its own — the
   operator's complimentary Gemini Pro grant, if you have one, is a separate additional
   allowance through ~mid-2027; revisit the free-tier assumption before it lapses).
2. Set `CLAV_LLM__API_KEY=your-key` in `.env`.
3. Tune `llm.model` (default `gemini-1.5-flash`), `llm.max_output_tokens`,
   `llm.timeout_seconds` in `config.yaml` if needed.
4. Raise `weights.llm` above `0.0` (and lower `weights.technical` to compensate — they must sum
   to `1.0`) so Gemini's signal actually contributes to `raw_score`; `config.example.yaml` ships
   a `0.7`/`0.3` starting split.

**Cost/budget knobs and the circuit breaker** (`llm:` block) — `daily_token_budget` (`0` disables
Gemini entirely, pure technical-only), `daily_cost_cap_usd` (`0.0` = no monetary cap, matching a
`$0` free-tier cost model — set `cost_per_1k_prompt_tokens_usd`/`cost_per_1k_completion_tokens_usd`
above `0` if you're on a paid tier and want a real spend cap), `breaker_failure_threshold` +
`breaker_cooldown_seconds` (opens after N consecutive failures, half-opens — one trial call —
after the cooldown, closes on success). All state changes are logged; a snapshot is persisted to
`system_control` (`llm_budget_snapshot`) each cycle and exposed at `GET /health` for `clav-web`.

**Stage-1 social filter thresholds** (`sources.social`) — `min_engagement_score`, `min_replies`,
`min_author_reputation` (karma/followers floor), `max_symbols_per_post` (cashtag-stuffing cap),
`near_dup_enabled` (coordinated-copypasta collapse), `anomaly_volume_multiplier` /
`low_liquidity_volume_multiplier` (mention-volume spike vs. rolling baseline → `anomaly_flag`,
a manipulation-risk signal rather than a bullish one), `min_posts_for_anomaly`. See
[epic-03](docs/epics/epic-03-gemini-and-control.md#bot--spam-defense-two-stage) for the full
two-stage rationale.

#### The decision journal — how it reads, and how to tune from it

Every non-`HOLD` decision is written to `trade_proposal` (`GET /api/journal`, or the `/` page in
the web UI): symbol, side, `status` (`executed` | `vetoed` | `pending` | `approved` | `rejected` |
`expired`), proposed/executed qty, `rationale`, and `inputs_ref` (the exact `news_item` /
`social_digest` / `analysis_result` row ids that fed the analysis). Click through to
`/journal/{id}` (or `GET /api/journal/{id}`) for the full why: the `decision` row's scores
(`raw_score`, `technical_score`, `llm_signal`), Gemini's `sentiment`/`conviction`/`rationale`/
`prompt_version`/`model` (in `decision.reasoning.llm`), and the `risk_evaluation` outcome
(approved/capped/vetoed, per-rule notes — same shape as the Epic-2 section above).

Full chain, joined by id: `news_item`(s) / `social_digest` → `analysis_result` (the **exact**
redacted Gemini request + response text) → `prompt_version` → `decision` → `risk_evaluation` →
`trade_proposal` → `order`. The `analysis_result_id` back-link lives in both
`decision.reasoning.llm` and `trade_proposal.inputs_ref`, so any closed paper trade is
reconstructable to the precise prompt Gemini saw and the precise JSON it returned.

**To tune from what you read:** open a journal entry, read the rationale and risk outcome, then
adjust `GET/PUT /api/config` (or the `/config` page) — weights, the risk-knob subset
(`max_position_value`, `max_daily_loss_pct`, `max_drawdown_pct`, `max_portfolio_exposure_pct`,
`max_sector_allocation_pct`, `cooldown_minutes`, `post_loss_cooldown_minutes`), or the watchlist.
Writes are validated with the exact same Pydantic models as boot-time `config.yaml` and take
effect on the **very next scan cycle** — no `clav-core` restart. (`scan_interval_minutes` is
persisted/validated but not live-rescheduled yet — a restart picks up a schedule change.)

#### The optional approval mode

Default `approval.mode: auto` — every risk-passing decision executes autonomously and lands in
the journal as `executed` (or `vetoed`). Set `approval.mode: manual` (globally) or add a
`per_symbol` override (e.g. `per_symbol: {TSLA: manual}`) to babysit one volatile name: a
passing BUY is written `pending` and does **not** execute until you approve it (via
`POST /api/journal/{id}/approve` or the journal page's Approve button) — it expires
(`approval.ttl_minutes`, default 30) and never executes if you don't. **Exits always execute
immediately in either mode** — nothing about approval mode ever traps you in a losing position.
Approve/reject from the web UI/API are **DB-only writes** (`clav-web` never holds brokerage
keys); `clav-core` performs the real broker submission on its very next cycle and reuses the
same idempotent `client_order_id` path as an auto-executed trade.

#### Starting `clav-web` and reaching the UI

```bash
# Dev, foreground:
uv run clav-web
# -> http://127.0.0.1:8080  (binds to localhost only by default)
```

```bash
# Pi, systemd (installed by deploy/install.sh alongside clav-core):
sudo systemctl start clav-web
sudo systemctl status clav-web
journalctl -u clav-web -f
```

By default `clav-web` binds to `127.0.0.1` — reachable only from the Pi itself (or via
`ssh -L 8080:localhost:8080 pi@<pi-ip>` for a quick tunnel). For real LAN access, set
`web.bind_host` in `config.yaml` to `0.0.0.0` (reachable from any device on your home network)
or to your [Tailscale](https://tailscale.com) IP (reachable from your phone anywhere, with
Tailscale itself as the authentication — no port-forwarding, nothing exposed publicly). Neither
needs an app password for a single operator (epic decision #7); optionally set
`CLAV_WEB__TOKEN` in `.env` for defence-in-depth on a shared/untrusted LAN — when set, it's
required on state-changing requests only (`X-Clav-Token` header for the JSON API, a hidden form
field for the HTML UI), never on reads.

#### Editing the persona

`GET/PUT /api/prompt` (or the `/prompt` page) edits Gemini's strategy prompt/persona. Editing
creates a new immutable version and activates it atomically — the previous version is retained
(`GET /api/prompt/versions` for history, `POST /api/prompt/versions/{id}/activate` to roll back).
`GeminiAnalyst` re-reads the active version on its very next call — no restart. A safe default
ships in `llm.default_persona` and seeds the store on first boot (never overwrites a later edit).

#### Gemini-driven vs. technical-only in the logs

```bash
journalctl -u clav-core -f | jq 'select(.event == "analyst_signal")'
# Gemini-driven:   {"event": "analyst_signal", "symbol": "AAPL", "sentiment": 0.8, "conviction": 0.7, "llm_signal": 0.56, "is_fallback": false, "news_count": 1, "social_anomaly": false, ...}
# Technical-only:  {"event": "analyst_signal", "symbol": "AAPL", "sentiment": 0.0, "conviction": 0.0, "llm_signal": 0.0, "is_fallback": true, ...}
```

`is_fallback: true` covers every degradation path (no key, timeout, malformed response,
out-of-range values, safety block, budget/breaker exhaustion) — the same neutral signal either
way, always logged with the reason at `warning` (`gemini_call_failed` / `gemini_response_invalid`).

### Epic 4 runbook (dashboard, alerting, and observability)

Everything here is **off by default and additive** — a fresh clone still runs with no new
config: `HealthMonitor` always writes `health_event` rows and `/health`/`/metrics` always work,
while alert channels stay disabled until you opt in. New knobs live in the `observability:` and
`alerts:` blocks of `config.example.yaml` (fully commented); alert channel credentials are
secrets, `.env`-only, never in `config.yaml`.

#### Starting the dashboard

Same process as Epic 3 — `clav-web` serves every page in this section (see
[Starting `clav-web` and reaching the UI](#starting-clav-web-and-reaching-the-ui) above for the
`uv run clav-web` / systemd commands and the bind-host/token model). Nothing in Epic 4 changes
who holds brokerage keys: `clav-web` still only reads the shared SQLite DB that `clav-core`
writes; `HealthMonitor` itself runs inside `clav-core` (it needs live broker/analyst/portfolio
state) and is the only new writer.

#### Reading each dashboard view

- **Dashboard (`/`)** — system-health tiles (liveness, Alpaca, Gemini, freshness, system,
  daily P&L vs. cap) at a glance, polling every 15s via `hx-get` (plain page reload still works
  with JavaScript off — HTMX is enhancement only), plus the existing e-stop/pause controls,
  positions summary, and decision journal from Epic 3.
- **Portfolio (`/portfolio`)** — inline-SVG equity and drawdown sparkline charts over the
  persisted `portfolio_snapshot` history, and the open-positions table with unrealized P&L
  marked to the **last successfully fetched close** (the `candle` table) — not a live quote,
  since `clav-web` never calls the broker.
- **Explanations (`/explanations`)** — every decision, filterable by symbol/action, with a
  compact conviction/fallback badge; click through to `/explanations/{id}` for the full
  provenance chain: the exact redacted Gemini request/response, the news/social inputs that fed
  it, the risk outcome, and the resulting order/fill/trade. This is the same `decision.
  reasoning.llm` back-link Epic 3 persisted — no new capture plumbing, just a view over it.
- **Audit (`/audit`)** — a searchable browser over the durable `audit_log` + `health_event`
  journal (not a log-file grep — verbose structured logs stay on disk/journald, see
  [Read the logs](#read-the-logs) above), filterable by cycle id/category/severity, newest-first
  and paginated. Click a cycle id to jump to `/audit/cycle/{id}`, a one-click "reconstruct this
  cycle" view joining that cycle's `config_snapshot` → decisions → risk evaluations → orders →
  health events → audit log in one place.
- **Calibration (`/calibration`)** — closed trades joined to the conviction that drove them: a
  conviction-vs-realized-P&L scatter plus a bucketed summary (mean return/hit-rate by
  |conviction| band, Gemini-driven vs. technical-only). Explicitly **descriptive** — it reads
  existing rows and adds no scored calibration model or review worker; the structured
  retrospective (backtesting/re-scoring past calls) is Epic 5.

#### What each health tile/alert means, and how to respond

| Tile / alert condition | Meaning | Respond by |
|---|---|---|
| `liveness` | Seconds since the last **completed** cycle; `unknown`/`ok` until the first cycle ever finishes | If stuck `warn`/`critical` for longer than a couple of scan intervals, check `journalctl -u clav-core` — the process may be stuck or crash-looping |
| `alpaca` / `gemini` (external) | Last call's success + circuit-breaker state | An open breaker degrades gracefully (Alpaca: no new orders; Gemini: technical-only) — usually self-heals after the cooldown; investigate if it stays open |
| `freshness` (per source) | Age of the latest quote/indicator/news/social pull vs. `observability.freshness_warn_hours` / `freshness_critical_hours` | A stale source degrades that input, never the whole cycle — check the source's own logs/rate limits |
| `system` | Process RSS, free memory, CPU load, SSD free, DB+WAL size vs. `observability.*` thresholds | `warn`/`critical` on a Pi usually means it's time to prune retention or check for a runaway process |
| `daily P&L vs. cap` | Today's realized+unrealized P&L against `risk.max_daily_loss_pct` | Approaching/at cap trips the daily-loss circuit breaker automatically — no action needed, it's informational |
| `broker_unreachable` (alert, critical) | Alpaca calls are failing | Check Alpaca status/API keys; trading pauses itself until it recovers |
| `llm_breaker_open` / `llm_budget_exhausted` (alert, warning) | Gemini calls are failing repeatedly, or the daily token/cost budget is spent | Trading continues technical-only; raise `llm.daily_token_budget`/`daily_cost_cap_usd` or wait for the next UTC day if this is expected |
| `drawdown_breach` / `daily_loss_cap_hit` (alert, critical) | A risk circuit breaker tripped | By design — new entries pause until the condition clears; review the Portfolio/Journal pages for what happened |
| `emergency_stop_tripped` (alert, critical) | The e-stop is active (fires once on the edge, not every cycle) | Clear it from `/` once you've confirmed it's safe to resume |
| `cycle_gap_exceeded` (alert, warning) | Cycles are running noticeably slower than `scan_interval_minutes` during market hours | Check for a slow data source or an overloaded Pi |

#### Configuring alert channels (SMTP/webhook, off by default)

Both channels are disabled unless you opt in — until then, every condition above still logs and
persists a `health_event`, it just never sends anywhere.

1. **Email** — set `alerts.smtp.enabled: true` plus `host`/`port`/`use_tls`/`from_addr`/
   `to_addr` in `config.yaml`, and (if your relay needs auth) `CLAV_ALERTS__SMTP__USERNAME` /
   `CLAV_ALERTS__SMTP__PASSWORD` in `.env`.
2. **Webhook** (ntfy/Telegram-relay-shaped: a plain JSON POST) — set `alerts.webhook.enabled:
   true` plus `url` in `config.yaml`, and `CLAV_ALERTS__WEBHOOK__TOKEN` in `.env` if the relay
   wants a bearer token.
3. Tune `alerts.critical_dedup_minutes` (don't repage the same condition within this window) and
   `alerts.digest_interval_minutes` (how often buffered WARNING alerts flush as one digest) to
   taste. See `config.example.yaml`'s `alerts:` block for the fully-commented defaults.

**Epic 6:** while `mode: live`, every alert above escalates to critical and sends immediately —
`llm_breaker_open`/`llm_budget_exhausted`/`cycle_gap_exceeded` included, even though they're
`warning` (batched) in paper. There's nothing to configure for this; it follows `mode` automatically.

Secrets never appear in a rendered alert or log line — the SMTP password only authenticates the
connection (`smtplib`'s `login()`), and the webhook token is sent solely as an `Authorization`
header, never embedded in the alert body/JSON payload a recipient actually reads.

#### Pointing an off-box Prometheus at `/metrics`

`GET /metrics` renders the same `health_snapshot` as `/health`, in Prometheus text exposition
format — scrape it from another machine (no bundled TSDB on the Pi itself):

```yaml
# prometheus.yml, on the scraping host
scrape_configs:
  - job_name: clav
    static_configs:
      - targets: ["<pi-ip-or-tailscale-ip>:8080"]
```

Per-ticker/per-source labels stay bounded to the watchlist size (never an unbounded label set),
so the scrape payload can't grow unboundedly as history accumulates.

#### Reproducing a decision: `config_snapshot` + `analysis_result`

Every cycle persists a `config_snapshot` row (the fully-resolved effective config — defaults +
`config.yaml` + any live `/config` override — plus the running git SHA), deduplicated by content
hash so an unchanged config across thousands of cycles doesn't bloat the DB. Combined with
Epic 3's `analysis_result` (the exact redacted Gemini request/response for that decision), any
historical trade is reproducible to the precise code, config, and prompt/response that produced
it: open `/audit/cycle/{cycle_id}` for the config, or `/explanations/{decision_id}` for the
Gemini call — both resolve straight from the ids already on the `decision` row, no extra lookup.

### Epic 5 runbook (trade review and calibration)

Everything here is **off by default in the sense that it needs no configuration** — a fresh
clone still reviews every closed paper trade with no `review:` block set in `config.yaml`, and
(like Epic 3's analysis) a missing Gemini key just means reviews defer forever rather than
error. New knobs live in the `review:` block of `config.example.yaml`; there are no new
secrets.

#### Starting and tuning the review pass

The review pass runs inside `clav-core` (same process as the scan cycle) as its own
`APScheduler` job — no separate command to start. Tune its cadence and retry behavior via
`config.yaml`:

```yaml
review:
  interval_minutes: 120 # off-peak-friendly: doesn't compete with scan-cycle Gemini calls
  max_attempts: 5 # a review that keeps failing gives up after this many tries
  backoff_base_seconds: 300 # first retry waits at least this long
  backoff_max_seconds: 21600 # backoff never waits longer than this (6h)
```

Reviews share the exact same `GeminiBudget`/circuit breaker as entry analysis (Epic 3) — there
is no separate review budget to size. On a very active trading day, entry analysis and reviews
draw from the same daily token/cost ceiling; if that ever starves one side, the fix is
`interval_minutes` or `llm.daily_token_budget`, not a new knob.

#### Reading the journal (`/reviews`)

Open `/reviews` for a list of every **closed** trade — not just ones already reviewed — each
tagged `reviewed`, `pending`, or `failed (N attempts)`. Click through to `/reviews/{trade_id}`
for the full post-mortem: why the trade was entered, what worked, what misleading signals
pointed the wrong way, a hindsight view, concrete improvement suggestions, and the model's own
`confidence_calibration` verdict (`overconfident` / `calibrated` / `underconfident`) — alongside
a link back to `/explanations/{decision_id}` (Epic 4) for the original entry rationale, so you
never have to piece the two together yourself.

What `review_status` means and how to respond:

| Status | Meaning | Respond by |
|---|---|---|
| `pending` | Not yet reviewed, or deferred because the shared Gemini budget/breaker was unavailable this pass | No action — it's retried on the next pass with no backoff for a defer, or you can watch `/health`'s Gemini breaker/budget tiles (Epic 4) if it stays pending for many passes |
| `reviewed` | A `trade_review` row exists; open the detail page to read it | — |
| `failed` | A genuine error (malformed response, timeout, safety block) recurred until `review.max_attempts` was reached | Check `clav-core`'s logs for `trade_review_attempt_failed`/`trade_review_failed_terminally` around that trade's closed_at; force a re-review once you believe the cause (e.g. a bad persona edit) is fixed |

#### Forcing a re-review

From the dashboard: open a `reviewed` or `failed` trade's detail page and click **Force a
re-review**. Via the API: `POST /api/reviews/{trade_id}/rerun` (same optional shared token as
every other state-changing route). Either way this is a **DB-only** reset of `review_status`
back to `pending` (attempts/backoff cleared) — `clav-web` never holds a Gemini key, so the
actual call happens on `clav-core`'s next scheduled pass. The trade's prior review(s) are never
deleted; a successful re-review simply appends a new, separately-dated row, and the detail page
shows the full history newest-first.

#### Calibration: is the model's stated confidence trustworthy?

`/calibration` (Epic 4) gains a second panel below the original conviction-vs-P&L scatter: an
`overconfident` / `calibrated` / `underconfident` breakdown of the LLM's own post-hoc verdict
against what the trade actually returned, plus tag and misleading-signal frequency tables drawn
from the journal. It's a different question from the panel above it — "did high conviction pay
off" (Epic 4, from `decision`) versus "was the model honest about its own confidence" (Epic 5,
from `trade_review`) — which is why the two stay visually separate rather than merged into one
table.

### Manual Pi hardware verification (deferred from Story 1.14)

The systemd units and `deploy/install.sh`/`deploy/backup.sh` were written and
`bash -n` syntax-checked against the docs, but this development environment has no
Raspberry Pi to run them on. Before relying on them for real, verify once on the actual
device:

1. **Reboot ⇒ auto-start ⇒ reconcile.** `sudo reboot`, then after it comes back:
   `systemctl is-active clav-core` should be `active`, and the first log lines should show
   `startup_reconciliation_begin` / `startup_reconciliation_complete`.
2. **Crash ⇒ restart ⇒ reconcile, no duplicate orders.** `sudo systemctl kill -s SIGKILL
   clav-core`, wait ~10s (`RestartSec`), confirm `systemctl status clav-core` shows it back
   up and reconciled, and that no order got double-submitted (check the `order` table for
   duplicate `client_order_id`s — should be structurally impossible per the UNIQUE
   constraint, but this is the real-hardware proof).
3. **Backup.** Run `sudo -u clav /opt/clav/deploy/backup.sh` manually once, confirm a
   `clav-*.db` file lands under `/opt/clav/backups/`, and that `sqlite3 <backup> "PRAGMA
   integrity_check;"` reports `ok`.
4. **DB/logs on the SSD, not the SD card** (docs/09-deployment.md §1) — confirm
   `data_dir`/`log_dir` in `config.yaml` point at the mounted SSD path before step 1.

---

## Adam section

This part is written for someone starting from a **half-installed, broken mess** on a
**Raspberry Pi 4** — nothing here assumes you remember what you already tried. Every step
is a command you copy, paste, and run one at a time. If a command prints something in red
or says `error`, stop and read the "If something breaks" box before moving on.

### Step 0 — Open a terminal on the Pi

If you're using the Pi with a screen/keyboard, open **Terminal** from the desktop menu.
If you're connecting from another computer, open a terminal there and type (replacing
`pi` and the IP address with your Pi's username and address):

```bash
ssh pi@<your-pi-ip-address>
```

Everything below happens inside that terminal window.

### Step 1 — Wipe the slate clean (safe even if nothing works yet)

A "half-installed" CLAV usually means a partial folder with missing pieces. We're not
going to guess what's broken — we're going to move it out of the way and start over. This
does **not** delete it, just renames it, so nothing is lost:

```bash
cd ~
mv clav clav-broken-backup-$(date +%Y%m%d) 2>/dev/null || true
```

That command means: "if a folder called `clav` exists, rename it to something like
`clav-broken-backup-20260720` so it's out of the way; if it doesn't exist, do nothing and
don't complain." Either way, you now have a clean spot to work in.

### Step 2 — Make sure the basic tools are installed

Copy-paste this whole block. It updates the Pi's software list and installs `git` (for
downloading the code) and `curl` (for downloading the installer in the next step):

```bash
sudo apt update
sudo apt install -y git curl
```

You'll be asked for your Pi's password when you type `sudo` — that's normal, type it and
press Enter (it won't show characters on screen, that's expected, just type it and hit
Enter).

### Step 3 — Install `uv` (the tool that manages Python for this project)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

Check it worked:

```bash
uv --version
```

You should see something like `uv 0.x.x`. If you instead see `command not found`, close
the terminal, open a brand new one, and try `uv --version` again — this refreshes the
terminal so it knows where `uv` was installed.

### Step 4 — Download CLAV fresh

```bash
cd ~
git clone https://github.com/traxon99/clav.git
cd clav
```

You are now standing inside the freshly downloaded project. Every command from here on
assumes you're in this `~/clav` folder — if you ever close and reopen the terminal, just
run `cd ~/clav` again first.

### Step 5 — Install the project's dependencies

```bash
uv sync --all-groups
```

This reads `pyproject.toml` and downloads everything CLAV needs into a private folder
(`.venv`) inside the project — it will not mess with anything else on your Pi. This can
take a few minutes on a Pi 4, especially the first time. Let it finish.

### Step 6 — Create your settings files

CLAV ships with two **template** files. You copy them to real files and then edit the
real ones — the templates themselves are never touched, so if you mess up your real
config, you can always copy the template again to start over.

```bash
cp config/config.example.yaml config/config.yaml
cp .env.example .env
```

### Step 7 — Get your Alpaca paper-trading keys

CLAV trades with **fake money** (paper trading) by default — this is safe, no real money
is ever at risk unless you deliberately change that later.

1. Go to https://app.alpaca.markets/paper/dashboard/overview and sign up / log in (free).
2. Find your **API Key ID** and **Secret Key** on that page.
3. Open the `.env` file you just created and put them in:

```bash
nano .env
```

You'll see two lines like this — replace the placeholder text after the `=` sign with
your real key and secret (leave everything else exactly as-is):

```
CLAV_ALPACA__API_KEY=your-alpaca-paper-key-id
CLAV_ALPACA__API_SECRET=your-alpaca-paper-secret-key
```

To save in `nano`: press `Ctrl+O`, then `Enter`, then `Ctrl+X` to exit.

### Step 8 — (Optional) Pick which stocks to watch

Open the config file:

```bash
nano config/config.yaml
```

Near the top there's a `watchlist:` section with stock symbols like `AAPL`, `MSFT`, etc.
You can leave the defaults or change them to whatever stocks you want CLAV to watch.
Save the same way: `Ctrl+O`, `Enter`, `Ctrl+X`.

### Step 9 — Set up the database

```bash
uv run alembic upgrade head
```

This builds the local database file CLAV needs to remember its trades. You should see a
few lines of output ending without any `ERROR` text.

### Step 10 — Prove it actually works

```bash
uv run pytest
```

This runs CLAV's built-in self-check (no internet or real trading involved). Wait for it
to finish — you want to see something like `XX passed` in green at the bottom, and no
`FAILED` lines. If you see failures, see the troubleshooting box below.

### Step 11 — Run it for the first time by hand

```bash
uv run clav core
```

If this is working, the terminal will start printing log lines and just sit there
running — that's correct, it's supposed to keep running forever, scanning the market on a
schedule. Press `Ctrl+C` to stop it once you've confirmed it started without crashing.

### Step 12 — Make it run automatically, all the time (recommended for a Pi)

This installs CLAV as a background service so it starts automatically every time the Pi
boots, and restarts itself if it ever crashes:

```bash
sudo ./deploy/install.sh
```

Check that it's actually running:

```bash
sudo systemctl status clav-core
```

You want to see the word **`active (running)`** in green. That means it's up.

To watch it work in real time:

```bash
journalctl -u clav-core -f
```

Press `Ctrl+C` to stop watching (this does **not** stop CLAV itself, it just stops
displaying the logs).

### If something breaks

- **`command not found: uv`** — close the terminal window completely, open a new one, and
  `cd ~/clav` before trying again. `uv` needs a fresh terminal to be recognized.
- **`sudo: command not found` or password rejected** — you need to be logged in as a user
  that has admin (`sudo`) rights on the Pi. The default Raspberry Pi user (`pi` or
  whatever you named it during setup) has this by default.
- **`uv sync` or `uv run` fails with a network error** — check the Pi's internet
  connection (`ping -c 3 google.com`); retry once it's back.
- **`pytest` shows `FAILED`** — do not run `sudo ./deploy/install.sh` yet. Go back to Step
  6/7 and double check `.env` and `config/config.yaml` were saved correctly, then re-run
  `uv run pytest`.
- **`systemctl status clav-core` shows `failed` or `inactive`** — read the last 20 lines
  of the log to see why: `journalctl -u clav-core -n 20 --no-pager`. The most common cause
  is a missing or misspelled key in `/opt/clav/.env` (Step 7) — that file is a *separate
  copy* from your project folder's `.env`, made by `install.sh`, so edit it directly:
  `sudo nano /opt/clav/.env`, then `sudo systemctl restart clav-core`.
- **Still stuck / truly half-broken again** — go back to Step 1 and start over. Nothing in
  this guide can lose your Alpaca keys (you can always fetch them again from Alpaca's
  website), so there's no harm in wiping and redoing it.
- **Emergency off switch** — if CLAV is running and you just want it to stop trading
  right now without uninstalling anything: `uv run clav-ctl estop-set` (or, on a Pi with
  the service installed: `sudo -u clav /opt/clav/.venv/bin/clav-ctl estop-set`).
