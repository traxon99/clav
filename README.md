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

## Status

Epic 1 ([Foundation & First Autonomous Paper Trade](docs/epics/epic-01-foundation.md)) is
implemented: a running skeleton that scans a watchlist, makes technical-only Buy/Sell/Hold
decisions, executes them idempotently against Alpaca **paper**, tracks the portfolio, and
persists a full provenance trail, with minimal guardrails and an emergency stop. News,
Gemini, the full 15-rule risk engine, the dashboard, and live trading are out of scope until
Epics 2–6 — `llm_signal` is hardcoded to `0`.

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
