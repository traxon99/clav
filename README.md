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

## Status

Epic 1 ([Foundation & First Autonomous Paper Trade](docs/epics/epic-01-foundation.md)) is
implemented: a running skeleton that scans a watchlist, makes technical-only Buy/Sell/Hold
decisions, executes them idempotently against Alpaca **paper**, tracks the portfolio, and
persists a full provenance trail, with minimal guardrails and an emergency stop.

Epic 2 ([Full Risk Engine, Volatility Sizing & Portfolio Accounting](docs/epics/epic-02-risk-and-portfolio.md))
is implemented: all 15 canonical risk rules run in order (`RiskEngine`/`default_rules()`),
ATR-based position sizing with stop-loss/take-profit (`PositionSizer`), a stop-monitor job that
exits independently of the decision path, a portfolio manager that computes real market-value
exposure/drawdown/sector allocation, a static sector map and earnings-calendar seed, and a
persisted `risk_evaluation` row for every decision (see the runbook section above). Still
paper-only, still `llm_signal = 0`.

Epic 3 ([Gemini Analyst, News, Social Sentiment & Human-Steerable Trading](docs/epics/epic-03-gemini-and-control.md))
is implemented: free-tier news (RSS + SEC EDGAR, optional off-by-default NewsAPI) and retail
social sentiment (Reddit + StockTwits) feed a `GeminiAnalyst` that **proposes** trades — with
sentiment, catalysts, conviction, and a written rationale — while the Epic-2 risk engine stays
the hard gate that vetoes and sizes every order. Social feeds are de-spammed by a **two-stage
funnel**: a deterministic Stage-1 filter + aggregation (engagement/reputation floors,
cashtag-stuffing/promo rejection, near-dup collapse, volume-vs-baseline anomaly guard) shrinks
the firehose to a compact per-symbol digest, then Gemini applies Stage-2 judgement (organic
enthusiasm vs. coordinated pump) — it never sees the raw feed, keeping the token cost inside a
free budget (`GeminiBudget`'s daily token/cost cap + consecutive-failure circuit breaker). Any
Gemini failure — timeout, malformed JSON, out-of-range values, safety block, budget exhaustion —
degrades to a neutral (`llm_signal = 0`) technical-only signal, never an exception; a prompt-
injection/social-manipulation chaos suite proves this under CI. Trades **execute autonomously**
once they pass the risk gate — no per-trade approval or notifications by default — and every
decision is written to a reviewable **decision journal** (`trade_proposal`: inputs → Gemini
rationale + prompt version → risk outcome → order, all joined by ids). A minimal `clav-web`
control API + HTMX UI (bound to localhost/LAN, optional shared token) lets the operator
*supervise and tune*: browse the journal, edit Gemini's persona/strategy prompt (hot-reloaded,
versioned), adjust weights/risk knobs/watchlist (live-applied on the next cycle, no restart),
and trip/clear an always-available e-stop/pause; per-symbol approve/reject is an **optional
off-by-default mode** for babysitting a volatile name — approvals are DB-only writes from the web
process, which never holds brokerage keys; `clav-core` performs the actual submission on its next
cycle. See [Runbook — Epic 3](#epic-3-runbook-gemini-newssocial-and-the-control-uiapi) below. Everything
runs on **free tiers** (no paid keys required); X/Twitter is excluded for lack of a free read
tier. Still paper-only; the rich dashboard and live trading remain Epics 4 and 6.

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
| `flatten_on_estop` | *(not yet wired)* | Reserved for a future epic — currently has **no effect**; tripping `emergency_stop` freezes new entries but does **not** auto-close open positions today |

`sector_map` (top-level, not under `risk:`) seeds `instrument.sector`; `earnings_calendar`
(top-level) is the static, config-provided earnings seed — see the commented examples in
`config.example.yaml`.

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
`expired`), proposed/executed qty, `rationale`, and `inputs_ref` (the exact `news_item`/
`social_digest` row ids that fed the analysis). Click through to `/journal/{id}` (or
`GET /api/journal/{id}`) for the full why: the `decision` row's scores (`raw_score`,
`technical_score`, `llm_signal`), Gemini's `sentiment`/`conviction`/`rationale`/`prompt_version`/
`model` (in `decision.reasoning.llm`), and the `risk_evaluation` outcome (approved/capped/vetoed,
per-rule notes — same shape as the Epic-2 section above).

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
