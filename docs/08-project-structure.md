# 08 — Project Structure, Packages & Libraries

## 1. Folder structure

```
clav/
├── README.md
├── pyproject.toml                # deps, tooling (ruff, mypy, pytest) config
├── config/
│   ├── config.example.yaml       # committed template
│   ├── config.yaml               # local, gitignored
│   └── watchlist.yaml
├── .env.example                  # API key names only (gitignored real .env)
├── docs/                         # this architecture set
├── deploy/
│   ├── clav-core.service         # systemd unit
│   ├── clav-web.service
│   ├── install.sh
│   └── backup.sh
├── migrations/                   # Alembic
├── src/
│   └── clav/
│       ├── __init__.py
│       ├── app.py                # composition root / entrypoints
│       ├── config.py             # Pydantic Settings
│       ├── clock.py              # Clock abstraction (SystemClock/FakeClock)
│       ├── domain/               # pure logic, NO vendor imports
│       │   ├── models.py         # Pydantic domain types
│       │   ├── indicators.py
│       │   ├── decision.py       # DecisionEngine
│       │   ├── risk/
│       │   │   ├── engine.py
│       │   │   ├── rules.py      # each RiskRule
│       │   │   └── sizing.py     # PositionSizer
│       │   └── portfolio.py
│       ├── interfaces/           # abstract base classes / Protocols
│       │   ├── market_data.py
│       │   ├── news.py
│       │   ├── analyst.py
│       │   └── broker.py
│       ├── integrations/         # adapters (vendor SDKs live ONLY here)
│       │   ├── alpaca_data.py
│       │   ├── alpaca_broker.py
│       │   ├── paper_broker.py
│       │   ├── dryrun_broker.py
│       │   ├── gemini_analyst.py
│       │   └── news/
│       │       ├── newsapi.py
│       │       ├── rss.py
│       │       ├── edgar.py
│       │       └── composite.py
│       ├── services/             # orchestration
│       │   ├── scan_cycle.py     # ScanCycleService
│       │   ├── execution.py      # ExecutionEngine
│       │   ├── review.py         # TradeReviewService
│       │   ├── scheduler.py      # APScheduler jobs
│       │   └── health.py         # HealthMonitor
│       ├── data/                 # persistence
│       │   ├── db.py             # engine/session, PRAGMAs
│       │   ├── tables.py         # SQLAlchemy models
│       │   └── repositories.py
│       ├── web/                  # dashboard (separate process)
│       │   ├── main.py           # FastAPI app
│       │   ├── routers/
│       │   ├── control.py        # guarded pause/stop/override
│       │   └── templates/        # HTMX/Jinja
│       └── common/
│           ├── logging.py
│           ├── retry.py          # backoff decorator
│           ├── circuit_breaker.py
│           └── errors.py
└── tests/
    ├── unit/
    ├── integration/
    ├── backtest/
    └── fixtures/                 # recorded API responses (VCR cassettes)
```

**The one rule that keeps this modular:** `domain/` and `interfaces/` may not import
anything from `integrations/`. Vendor SDKs are quarantined to `integrations/`. Enforce it
with an import-linter contract in CI.

## 2. Package recommendations

### Core runtime
| Concern | Library | Notes |
|---------|---------|-------|
| Broker + data | `alpaca-py` | Official Alpaca SDK |
| LLM | `google-generativeai` | Gemini Pro |
| Config | `pydantic`, `pydantic-settings` | Typed config + validation |
| Scheduling | `APScheduler` | In-process cron; light on RAM |
| DB / ORM | `SQLAlchemy` (2.x) + `alembic` | SQLite now, Postgres later |
| HTTP | `httpx` | Async-capable, timeouts, retries |
| Indicators | `pandas-ta` or hand-rolled numpy | See RAM note below |
| Data frames | `pandas` (or `polars`) | Keep working sets small on 2 GB |
| News parsing | `feedparser`, `beautifulsoup4` | RSS + HTML |
| Web | `fastapi`, `uvicorn`, `jinja2` + HTMX | Light server-rendered UI |
| Logging | `structlog` | Structured JSON logs |
| Resilience | `tenacity` | Retry/backoff (or the custom decorator) |
| Validation | `pydantic` | Also validates LLM JSON output |

### Dev / quality
`pytest`, `pytest-cov`, `freezegun` (time), `vcrpy`/`respx` (record HTTP), `ruff`
(lint+format), `mypy` (types), `import-linter` (layer boundaries), `hypothesis` (property
tests for risk math).

## 3. Raspberry Pi / 2 GB RAM notes
- **`pandas` is the heaviest dependency.** Options, cheapest-RAM first: hand-roll the ~6
  indicators in numpy (recommended — tiny, fast, exact), or use `pandas-ta`, or `polars`
  (lower memory than pandas for the same ops). Avoid `TA-Lib` (C build pain on ARM).
- **No SPA build chain.** HTMX + Jinja avoids Node/webpack entirely.
- **Keep working sets small:** fetch only the candles you need (e.g. last 200), never load
  full history into memory. Stream/paginate DB reads in the dashboard.
- **Two processes, not many.** `clav-core` + `clav-web` fit comfortably; avoid a broker/bus
  process for now.
- Pin versions and build wheels where possible; prefer piwheels for ARM.
