# CLAV — Continuous LLM-Assisted Valuation

CLAV is an autonomous, AI-assisted **swing-trading** system designed to run 24/7 on a
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

This repository currently contains the **architecture specification only**. Implementation
follows the [roadmap](docs/12-roadmap.md), starting with Phase 0 (skeleton + paper broker).
