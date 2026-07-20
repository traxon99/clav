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
| 3 | [Gemini Analyst, News, Social Sentiment & Human-Steerable Trading](docs/epics/epic-03-gemini-and-control.md) | Free-tier news (RSS/EDGAR) + social-sentiment (Reddit/StockTwits, deterministically de-spammed) feeding a `GeminiAnalyst` (strict-JSON, neutral fallback, cost breaker) that proposes trades behind the risk gate, an approval queue, and a minimal web control UI to steer it (Roadmap Phase 3) |

## Status

Epic 1 ([Foundation & First Autonomous Paper Trade](docs/epics/epic-01-foundation.md)) is
implemented: a running skeleton that scans a watchlist, makes technical-only Buy/Sell/Hold
decisions, executes them idempotently against Alpaca **paper**, tracks the portfolio, and
persists a full provenance trail, with minimal guardrails and an emergency stop. News,
Gemini, the full 15-rule risk engine, the dashboard, and live trading are out of scope until
Epics 2–6 — `llm_signal` is hardcoded to `0`.

Epic 2 ([Full Risk Engine, Volatility Sizing & Portfolio Accounting](docs/epics/epic-02-risk-and-portfolio.md))
is in progress: the full 15-rule pipeline, ATR-based position sizing with stop-loss/take-profit,
a portfolio manager that computes real exposure/drawdown/sector allocation, and a persisted
`risk_evaluation` audit trail — still paper-only, still `llm_signal = 0`. Stories 2.1 (risk
config + audit schema) and 2.2 (portfolio accounting) have landed; the nine remaining rules,
the `PositionSizer`, the stop-monitor, and full-pipeline persistence are outstanding.

Epic 3 ([Gemini Analyst, News, Social Sentiment & Human-Steerable Trading](docs/epics/epic-03-gemini-and-control.md))
is scoped (not started): free-tier news (RSS + SEC EDGAR) and retail social sentiment
(Reddit + StockTwits) feed a `GeminiAnalyst` that **proposes** trades — with sentiment,
catalysts, conviction, and a written rationale — while the Epic-2 risk engine stays the hard
gate that vetoes and sizes every order. Social feeds are de-spammed by a **two-stage funnel**:
deterministic Stage-1 filtering + aggregation (engagement/reputation floors, dedup, volume
baselines) shrinks the firehose to a compact per-symbol digest, then Gemini applies Stage-2
judgement (organic enthusiasm vs. coordinated pump) — it never sees the raw feed, keeping the
token cost inside a free budget. It adds a token/cost breaker, an approve-before-execute queue,
and a minimal authenticated web control UI to edit Gemini's strategy prompt, tune weights/risk,
manage the watchlist, and approve trades. Everything runs on **free tiers** (no paid keys);
X/Twitter is excluded for lack of a free read tier. Still paper-only; the rich dashboard and
live trading remain Epics 4 and 6. Epic 3 depends on Epic 2 being complete before Gemini is
wired into live decisions.

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
