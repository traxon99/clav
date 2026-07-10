# 10 — Observability, Logging & Alerting

## 1. Logging strategy

- **Structured JSON logs** via `structlog`. Every log line carries a **correlation id**
  (`cycle_id`, and `decision_id`/`order_id` where relevant) so an entire scan cycle can be
  reconstructed with one query.
- **Levels:**
  - `DEBUG` — raw payloads, indicator math (off in prod by default).
  - `INFO` — cycle start/end, decisions, orders, fills, health checks.
  - `WARNING` — degraded mode (stale data, LLM neutralized, source skipped, memory pressure).
  - `ERROR` — failed orders, broker errors, unhandled exceptions.
  - `CRITICAL` — emergency stop tripped, reconciliation failure, repeated auth failure.
- **Two sinks:** rotating JSON files on the SSD (`RotatingFileHandler`, size-capped) **and**
  journald (via systemd). The DB is *not* the log store — it holds durable domain records
  (decisions, reviews, health_events); verbose logs stay in files.
- **Redaction:** a logging processor strips API keys/tokens from any log record. Secrets must
  never appear in logs.

### What every decision logs (the audit trail)
Because `decision`, `risk_evaluation`, `analysis_result`, `order`, and `fill` are persisted
rows, the "explain this trade" requirement is satisfied by data, not by scraping logs. Logs
are the *operational* view; the DB is the *authoritative* journal. Both share the
correlation id.

## 2. Metrics & health

`HealthMonitor` runs each cycle and writes `health_event` rows + exposes a `/health` and
`/metrics` endpoint on the web service:

- **Freshness:** age of latest quote/indicator/news per ticker.
- **External services:** success/error rates and circuit-breaker state for Alpaca, Gemini,
  each news source; LLM latency and token spend vs monthly budget.
- **System:** process RSS, system free memory, CPU load, disk free on the SSD, DB size/WAL
  size.
- **Trading:** cycles completed, decisions by action, orders submitted/filled/rejected,
  current drawdown, daily P&L vs the daily-loss cap.
- **Liveness:** last successful cycle timestamp (a stale value = core is stuck/dead).

## 3. Alerting

- **Channels:** start with email (the operator's address) and/or a webhook (ntfy/Telegram);
  keep it pluggable.
- **Trigger conditions:** emergency stop tripped, daily-loss cap hit, broker auth failure,
  reconciliation failure, no successful cycle in > N minutes during market hours, disk/memory
  pressure, token budget exhausted.
- **Severity-gated:** `WARNING` batches into a periodic digest; `CRITICAL` pages immediately.
- Alerts are also `health_event` rows, so the dashboard shows history.

## 4. Dashboard as the human observability surface
The dashboard renders, from the DB: portfolio & positions, recent trades with their AI
explanations and confidence, per-ticker latest analysis, market summaries, system-health
tiles (freshness/breakers/memory), the daily-loss gauge, and a **searchable log/audit view**
filterable by `cycle_id`/symbol. Manual controls (pause/stop/override/flatten) are guarded,
confirmed, and written to `audit_log`.

## 5. Reproducibility
`config_snapshot` stores the effective config (+ git SHA) per cycle. Combined with the
append-only decision/analysis tables and recorded API responses, any historical decision can
be replayed and explained months later — the core engineering goal of the project.
