# 12 — Development Roadmap

Phased so that **something safe and testable exists early**, and every phase ships a working
system. Money-touching capability arrives only after the safety and observability layers are
proven in paper mode.

```mermaid
flowchart LR
    P0[Phase 0\nSkeleton] --> P1[Phase 1\nData + Paper loop]
    P1 --> P2[Phase 2\nRisk + Portfolio]
    P2 --> P3[Phase 3\nNews + Gemini]
    P3 --> P4[Phase 4\nDashboard + Obs]
    P4 --> P5[Phase 5\nReview journal]
    P5 --> P6[Phase 6\nLive gate + soak]
    P6 --> P7[Phase 7\nExpansion]
```

## Phase 0 — Skeleton & guardrails
- Repo, `pyproject`, layered folders, config (Pydantic Settings), logging, SQLite + Alembic
  baseline, `Clock` abstraction.
- Interfaces defined (`Broker`, `MarketDataSource`, `NewsSource`, `Analyst`).
- `DryRunBroker` + `PaperBroker`; composition root; systemd units.
- CI: pytest, ruff, mypy, import-linter contract.
- **Exit criteria:** a scheduled no-op cycle runs, logs, and persists a `scan_cycle` row.

## Phase 1 — Market data + paper trading loop
- `AlpacaDataAdapter` (quotes/candles/clock), `IndicatorService`, `DecisionEngine`
  (technical-only).
- End-to-end BUY/SELL/HOLD in **paper** mode with idempotent orders + fill reconciliation.
- **Exit criteria:** system autonomously paper-trades a watchlist on technicals; full
  provenance persisted; backtest harness runs the real engine.

## Phase 2 — Risk engine & portfolio manager
- All `RiskRule`s, `PositionSizer`, `PortfolioManager` (exposure/drawdown/sector), startup
  reconciliation, emergency stop + pause via `system_control`.
- Property tests for risk invariants.
- **Exit criteria:** no trade reaches the broker without a passing `RiskDecision`; safety
  invariants green in CI.

## Phase 3 — News + Gemini analysis (advisory signal)
- News adapters (NewsAPI/RSS/EDGAR) + dedup/cache; `GeminiAnalyst` with strict JSON +
  validation + neutral fallback; `llm_signal` wired into scoring with configurable weights;
  token budget + circuit breaker.
- **Exit criteria:** LLM enriches decisions but its failure never blocks or distorts trading
  (chaos test proves technical-only degradation).

## Phase 4 — Dashboard & observability
- FastAPI + HTMX dashboard (portfolio, positions, trades, AI explanations, confidence,
  health, logs); guarded controls; `/health` + `/metrics`; alerting.
- **Exit criteria:** operator can observe and safely control the running system remotely
  (over Tailscale/SSH).

## Phase 5 — Trade review journal
- `TradeReviewService` + review worker; aggregation/tags/calibration views in the dashboard.
- **Exit criteria:** every closed paper trade gets a structured review; journal is
  searchable.

## Phase 6 — Live-trading gate & soak
- Live config gate, LIVE banner, flatten-on-estop; multi-day **paper soak** then a small,
  capital-capped live pilot.
- **Exit criteria:** clean soak (no dup orders, no unhandled errors, green health); reviewed
  go-live checklist signed off.

## Phase 7 — Expansion
- Pick from [14 — Future Expansion](14-future-expansion.md): multi-agent, multi-broker,
  crypto, ML models, local LLM, distributed. Each is additive behind existing interfaces.

## Suggested sequencing principle
Ship **safety and observability before capability**. It is always correct to have a system
that trades conservatively and explains itself, and never correct to have one that trades
aggressively and cannot.
