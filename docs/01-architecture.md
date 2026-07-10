# 01 — System Architecture

## 1. Overall architecture

CLAV is a single-process-per-role monolith-of-services running on one Raspberry Pi. Roles
communicate through the SQLite database and an in-process event/scheduler layer rather than
a network message bus (kept deliberately simple for a 2 GB device; a bus is a documented
future step). The dashboard runs as a separate lightweight process so a UI crash can never
take down trading.

```mermaid
flowchart TB
    subgraph EXT["External services (network)"]
        ALP["Alpaca\nMarket data + Broker"]
        GEM["Gemini Pro API"]
        NEWS["News APIs\n(NewsAPI / RSS / EDGAR)"]
        SOC["Reddit / X\n(optional, flagged)"]
    end

    subgraph PI["Raspberry Pi 4 (2 GB) — Linux Mint"]
        subgraph CORE["clav-core (systemd service)"]
            SCHED["Scheduler\n(APScheduler)"]
            MDC["Market Data\nCollector"]
            NC["News Collector"]
            AIE["AI Analysis\nEngine"]
            DE["Decision Engine"]
            RISK["Risk Engine\n(deterministic gates)"]
            EXE["Execution Engine"]
            PM["Portfolio Manager"]
            REV["Trade Review\nWorker"]
            HEALTH["Health Monitor"]
        end

        subgraph WEB["clav-web (systemd service)"]
            API["FastAPI backend"]
            UI["Dashboard (HTMX/JS)"]
        end

        DB[("SQLite\n(WAL mode)")]
        LOGS[("Structured logs\n/var/log/clav")]
    end

    ALP <--> MDC
    ALP <--> EXE
    GEM <--> AIE
    GEM <--> REV
    NEWS --> NC
    SOC -.-> NC

    SCHED --> MDC & NC & AIE & DE & REV & HEALTH
    MDC --> DB
    NC --> DB
    AIE --> DB
    DE --> RISK --> EXE --> PM --> DB
    DE --> DB
    REV --> DB
    HEALTH --> DB & LOGS
    CORE --> LOGS

    API --> DB
    API --> CORE
    UI --> API
```

### Process topology
- **`clav-core`** — the trading brain. One Python process, single scheduler, modules called
  as in-process components. Restarted by systemd on crash.
- **`clav-web`** — FastAPI + a minimal server-rendered dashboard. Read-mostly; the few write
  actions (pause/stop/override) go through a guarded control API into `clav-core` via a
  small local IPC (a control table in SQLite polled by core, or a Unix socket).
- **SQLite** — the shared source of truth, opened in WAL mode so the reader (web) never
  blocks the writer (core).

## 2. Module diagram

```mermaid
flowchart LR
    subgraph Collectors
        MDC[MarketDataCollector]
        NC[NewsCollector]
    end
    subgraph Intelligence
        AIE[AIAnalysisEngine]
        IND[IndicatorService]
    end
    subgraph Decisioning
        DE[DecisionEngine]
        RISK[RiskEngine]
        SIZE[PositionSizer]
    end
    subgraph Trading
        EXE[ExecutionEngine]
        BRK[BrokerInterface]
        PM[PortfolioManager]
    end
    subgraph Reflection
        REV[TradeReviewService]
    end
    subgraph Platform
        REPO[Repositories / DB]
        CFG[ConfigService]
        LOG[LoggingService]
        SCH[Scheduler]
        HM[HealthMonitor]
    end

    MDC --> IND --> DE
    MDC --> REPO
    NC --> AIE --> DE
    NC --> REPO
    AIE --> REPO
    PM --> DE
    DE --> RISK
    RISK --> SIZE --> EXE
    EXE --> BRK
    EXE --> PM
    EXE --> REPO
    PM --> REPO
    REV --> REPO
    AIE --> REV
    SCH --> MDC & NC & AIE & DE & REV & HM
    CFG --> DE & RISK & SIZE & EXE
    LOG -.-> MDC & NC & AIE & DE & RISK & EXE & PM & REV
    HM --> REPO
```

Each module depends on **interfaces**, not concretions (see
[05 — Class Design](05-class-design.md)). This is what makes brokers, data sources, and the
LLM swappable.

## 3. Data-flow diagram

The canonical scan cycle. Deterministic stages are solid; the single LLM hop is dashed to
emphasize it is advisory and failure-tolerant.

```mermaid
flowchart TD
    START([Scheduler fires scan cycle]) --> GATE{Market open?\nEmergency stop clear?}
    GATE -- no --> IDLE[Log + sleep to next cycle]
    GATE -- yes --> FETCH[Fetch prices + candles per ticker]
    FETCH --> INDIC[Compute technical indicators]
    FETCH --> RAWNEWS[Fetch news + filings per ticker]
    RAWNEWS --> DEDUP[Dedupe + filter news]
    DEDUP -. bundle text .-> LLM[[Gemini Pro:\nsummary + catalysts + confidence JSON]]
    LLM -. structured JSON .-> SIGNAL[Build llm_signal]
    LLM -. failure/timeout .-> NEUTRAL[llm_signal = 0 neutral]

    INDIC --> SCORE[Decision Engine:\nweighted raw_score]
    SIGNAL --> SCORE
    NEUTRAL --> SCORE
    PORT[Portfolio state] --> SCORE

    SCORE --> CAND[Candidate action + target size]
    CAND --> RISKP[Risk pipeline\nveto / shrink only]
    RISKP -- blocked --> LOGHOLD[Record HOLD + reason]
    RISKP -- approved --> ORDER[Execution Engine:\nvalidate + submit order]
    ORDER --> BROKER[(Alpaca paper/live)]
    BROKER --> FILL[Fill / reject]
    FILL --> UPDATE[Portfolio Manager updates positions/P&L]
    UPDATE --> PERSIST[(Persist everything)]
    LOGHOLD --> PERSIST
    PERSIST --> CLOSECHK{Position closed?}
    CLOSECHK -- yes --> QREV[Queue trade for Gemini review]
    CLOSECHK -- no --> DONE([End cycle])
    QREV --> DONE
```

### Why this shape
- **News/LLM runs in parallel** with indicator computation, so LLM latency does not stall
  price collection.
- **The LLM feeds scoring, never execution.** There is no arrow from the LLM to the broker.
- **Risk is a mandatory choke point** between decision and execution — a single place to
  audit and test every safety rule.
- **Review is asynchronous**, off the hot path, so reflection never delays trading.

## 4. Layering

```
┌──────────────────────────────────────────────┐
│ Presentation   dashboard (FastAPI + HTMX)     │
├──────────────────────────────────────────────┤
│ Orchestration  scheduler, scan-cycle service  │
├──────────────────────────────────────────────┤
│ Domain         decision, risk, sizing,        │
│                portfolio, review              │
├──────────────────────────────────────────────┤
│ Integration    broker, market data, news,     │
│                Gemini clients (adapters)      │
├──────────────────────────────────────────────┤
│ Platform       config, logging, repositories, │
│                SQLite, health                 │
└──────────────────────────────────────────────┘
```

Dependencies point **downward only**. The domain layer knows nothing about Alpaca or
Gemini — it talks to `Broker`, `MarketDataSource`, `NewsSource`, and `Analyst` interfaces.
This is the single most important rule for keeping CLAV modular and testable.
