# 13 — Flows & Trade Lifecycle

## 1. Example request/response flow (one scan cycle)

```mermaid
sequenceDiagram
    autonumber
    participant SCH as Scheduler
    participant SC as ScanCycleService
    participant MD as MarketData
    participant NW as News
    participant AI as GeminiAnalyst
    participant DE as DecisionEngine
    participant RK as RiskEngine
    participant EX as ExecutionEngine
    participant BR as Alpaca(paper)
    participant PM as PortfolioManager
    participant DB as DB

    SCH->>SC: run(cycle_id) [every 30 min, market hours]
    SC->>MD: get_candles/quotes(watchlist)
    MD->>BR: REST bars/quotes
    BR-->>MD: OHLCV
    MD->>DB: persist quotes/candles/indicators
    SC->>NW: fetch(new since last)
    NW->>DB: persist deduped news
    SC->>AI: analyze(ticker, news) [only tickers w/ new news]
    AI-->>SC: AnalysisResult(confidence=0.62) or NEUTRAL
    SC->>DE: decide(indicators, llm_signal, portfolio)
    DE-->>SC: TradeDecision(BUY AAPL x10, score=0.48)
    SC->>RK: evaluate(decision, portfolio, market)
    RK-->>SC: RiskDecision(approved, qty=8)  %% shrunk by exposure cap
    SC->>EX: execute(risk_decision)
    EX->>BR: submit_order(client_order_id=clav-<cycle>-AAPL-buy, qty=8)
    BR-->>EX: accepted → filled @ price
    EX->>PM: apply_fill
    PM->>DB: update position, snapshot, trade
    SC->>DB: finalize scan_cycle
```

Note the shrink at step 12: the LLM was bullish, but the **exposure cap deterministically
reduced size from 10 to 8** — the risk engine, not the LLM, had the final word.

## 2. Full trade lifecycle (entry → hold → exit → review)

```mermaid
stateDiagram-v2
    [*] --> Candidate: DecisionEngine emits BUY
    Candidate --> Vetoed: RiskEngine blocks
    Vetoed --> [*]: logged as HOLD + reason
    Candidate --> Approved: all rules pass (maybe shrunk)
    Approved --> Submitted: ExecutionEngine sends order
    Submitted --> Rejected: broker rejects
    Rejected --> [*]: logged, alert
    Submitted --> Open: fill received → position opened\nstop & take-profit set
    Open --> Open: each cycle: re-score, stop-monitor, reconcile
    Open --> Closing: SELL signal / stop / take-profit / manual / risk-forced
    Closing --> Closed: exit fill → realized P&L computed
    Closed --> Reviewed: async Gemini review → journal entry
    Reviewed --> [*]
```

### Narrative walk-through
1. **Entry decision.** Technicals give +0.3; Gemini reports bullish, confidence 0.62, mixed
   sources → `llm_signal ≈ +0.43`; weighted `raw_score = 0.48` > buy threshold → candidate
   BUY.
2. **Risk gate.** Hours ✓, data fresh ✓, daily-loss ✓, no earnings blackout ✓, but portfolio
   exposure cap trims qty 10→8. `RiskDecision(approved, qty=8)` persisted with the cap
   reason.
3. **Execution.** Deterministic `client_order_id` submitted to Alpaca paper; fill received;
   `position` opened with stop (2×ATR) and take-profit stored.
4. **Hold.** Each cycle re-scores the name, the independent **stop-monitor** checks the stop,
   and the portfolio reconciles against the broker.
5. **Exit.** A stop-loss trips (or a SELL signal, take-profit, manual, or risk-forced
   exit). Exit order submitted; `trade` closed with realized P&L and exit reason.
6. **Review.** The closed trade is queued; Gemini writes a structured post-mortem
   (why entered, what worked, misleading signals, improvements) stored as a `trade_review`.
7. **Learning.** The dashboard aggregates review tags and confidence calibration; the
   operator uses these to tune weights/rules (never the LLM itself).

## 3. Degraded-mode flow (LLM/news/broker failure)

```mermaid
flowchart TD
    START([Scan cycle]) --> N{News fetch ok?}
    N -- no --> NEU[llm_signal = 0]
    N -- yes --> L{Gemini ok & valid JSON?}
    L -- no --> NEU
    L -- yes --> SIG[llm_signal from confidence]
    NEU --> SCORE[Score technical-only]
    SIG --> SCORE
    SCORE --> RISK[Risk pipeline fail-closed]
    RISK --> B{Broker reachable?}
    B -- no --> PARK[Park order pending + retry\nno assumption of success]
    B -- yes --> SUBMIT[Idempotent submit]
    PARK --> END([End])
    SUBMIT --> END
```

The invariant across all failure modes: **the system trades more conservatively or not at
all — it never trades more aggressively because information is missing.**
