# 14 — Future Expansion

The architecture is designed so each of these is **additive behind an existing interface**,
not a rewrite. Listed with the seam that makes it possible.

## Multiple AI agents
- **Seam:** the `Analyst` interface. Introduce an `AgentEnsemble` implementing `Analyst`
  that fans out to specialized agents (news analyst, technical commentator, risk skeptic,
  macro analyst) and aggregates their structured outputs.
- Aggregation stays deterministic: agents produce signals/confidences; a combiner (weighted
  vote, or a "debate then judge" step) yields a single `AnalysisResult`. The Decision Engine
  is unchanged — it still consumes one `llm_signal`.

## Multiple brokers
- **Seam:** the `Broker` interface + `broker_factory`. Add `IBKRBroker`, `TradierBroker`,
  etc. A `RoutingBroker` can select a venue per instrument. Portfolio Manager already
  reconciles against "the broker"; extend to per-broker sub-accounts.

## Multiple exchanges / global markets
- Extend `instrument` with exchange/currency/session metadata; `TradingHoursRule` becomes
  session-aware per exchange. Data/broker adapters per venue.

## Cryptocurrency
- **Seam:** same `Broker` + `MarketDataSource` interfaces (Alpaca even offers crypto; or add
  a CCXT-based adapter). Key differences handled in adapters/config: 24/7 sessions (relax
  `TradingHoursRule`), fractional sizing, different fee model. Risk engine is asset-agnostic.

## Custom ML models (XGBoost / LightGBM)
- **Seam:** the technical-score stage of `DecisionEngine`. Add a `ModelScorer` strategy that
  loads a trained model and outputs a score in `[-1,1]`, blended via a new weight. Training
  is offline; inference is light enough for the Pi (or precomputed off-device). Backtest
  harness already exists to validate before enabling.

## Reinforcement learning
- Longer-term. The backtest/replay environment + append-only decision/outcome tables are
  effectively an offline RL dataset. An RL policy would slot in as another `DecisionEngine`
  strategy, **still subject to the unchanged risk engine** — RL proposes, risk disposes.

## Transformer sentiment model (FinBERT) — considered and deferred
- **Seam:** the `SentimentScorer` interface (`clav/interfaces/sentiment.py`). A
  `FinBertScorer` would drop in beside `LexiconScorer` with no other change.
- **Why not now:** a quantized int8 ONNX FinBERT is ~66 MB of weights plus an
  `onnxruntime` runtime and a tokenizer, against a documented 150–350 MB `clav-core`
  budget on a 2 GB Pi (see [09 — Deployment](09-deployment.md) §2). It would consume
  most of the headroom `HealthMonitor`'s memory-pressure check exists to protect, and
  the marginal gain over VADER+overlay is small *because Stage 1 is not where nuance
  belongs* — Gemini already performs the nuanced Stage-2 read on the digest. Stage 1
  only has to be cheap, deterministic, and not wrong about negation.
- **If revisited:** benchmark real RSS and per-post latency on the Pi first, and gate
  loading behind the health monitor's pressure threshold so it can never be the reason
  a cycle is skipped.

## Local LLM support
- **Seam:** the `Analyst` interface. Add a `LocalLlmAnalyst` (e.g. Ollama) for cheap/offline
  analysis. On a 2 GB Pi this likely runs on a companion device; the interface hides where
  inference happens. Enables a hybrid: local model for routine summaries, Gemini for hard
  cases — chosen by a policy/router.

## Distributed execution
- **Seam:** replace in-process calls with a message bus (Redis Streams / NATS) between
  modules; move the DB to Postgres (already abstracted via SQLAlchemy). Collectors could run
  on the Pi while heavier analysis runs elsewhere. The module boundaries defined today are
  exactly the service boundaries you would cut along.

## Strategy experimentation / auto-tuning
- A backtest-gated experiment runner could propose weight/threshold changes from
  trade-review aggregates, validate them on historical data, and surface a diff for **human
  approval**. The system may suggest changes to itself; it may not silently apply changes to
  its own risk limits.

---

### Guiding constraint for all expansion
No future feature may move risk, sizing, or order-safety decisions into a model. New
intelligence always enters as **advisory input to the deterministic pipeline**. That single
rule is what keeps CLAV auditable no matter how sophisticated the analysis layer becomes.
