# Epic 7 — Autonomous Sentiment-Driven Discovery & On-Demand Analysis

> **Goal:** Let CLAV behave like an investment manager — **find** tickers to trade from live
> news/social sentiment across Alpaca's whole tradeable universe, instead of only scanning a
> fixed, human-curated watchlist — and let the operator hand it a specific ticker to analyze on
> demand. The watchlist doesn't go away; it becomes **pins** (names always watched) on top of
> whatever the bot discovers.
>
> Still **paper-only** (live remains Epic 6). Discovery ships **off by default**.

## The core constraint → a funnel, not a sweep

Running the full news+social+Gemini pipeline over ~11k assets every cycle is impossible: it
would blow the Gemini daily token budget (the cost breaker would trip in seconds), hammer the
rate-limited free news/social endpoints for hours, and exhaust the 2 GB Pi. So discovery is a
**funnel**:

1. A cheap, keyless **buzz pre-filter** (StockTwits trending — one call, market-wide) surfaces
   ~30 candidate tickers with a normalized buzz score. No per-symbol fan-out.
2. `DiscoveryService` validates them against the cached Alpaca catalog, drops pins + open
   positions + anything below the buzz floor, ranks, and takes the top
   `max_candidates_per_cycle` (default 8).
3. Only that bounded shortlist reaches the existing expensive analyst
   (`AnalystGateway.signal_for`) and the identical `_process_symbol` path.

## Resolved design decisions

1. **Autonomy — `full auto-trade`.** Names the bot discovers itself route through the *same risk
   gate* as watchlist names and execute (paper), with no approval step. This was the operator's
   explicit choice. The safety argument: discovered names are subject to the identical risk
   engine (position/exposure/sector caps, daily-loss/drawdown, cooldowns, e-stop/pause veto), the
   Gemini budget breaker, and the funnel cap — see Safety below.
2. **Discovery is a shortlist funnel, never a universe sweep** (above). `max_candidates_per_cycle`
   is the single honest guard: because discovered names are all *new* positions, it bounds both
   Gemini spend and how many fresh discovered entries a cycle can open.
3. **Two-process boundary preserved.** `clav-web` holds no Gemini/broker keys. On-demand
   "analyze this ticker" requests cross to `clav-core` through the DB (`analysis_request` table),
   exactly like e-stop/config — `clav-web` enqueues, `clav-core` drains and runs the pipeline.
4. **Asset catalog is a separate cache, not `instrument`.** `instrument` is created lazily only
   for symbols the bot touches; the `asset` table is the full tradeable catalog, refreshed on a
   slow cadence (`asset_universe.refresh_hours`), used to validate symbols and power ticker
   search. Empty catalog (fresh install / no Alpaca key) never vetoes — the UI falls back to a
   curated ticker list and discovery/on-demand still run.
5. **Sources are fail-open**, like the Epic-3 social adapters: a dead/blocked/rate-limited source
   contributes no candidates, never an exception that aborts a cycle.

## What was built

- **Config** (`config.py`): `sources.discovery` (enabled, sources, `max_candidates_per_cycle`,
  `min_score`, `exclude_open_positions`), `on_demand`, `asset_universe`; a live
  `RuntimeOverrides.discovery_enabled` toggle.
- **Data** (`tables.py` + migration `b1d7c0a5e2f4`): `asset` and `analysis_request` tables;
  `AssetUniverseRepository`, `AnalysisRequestRepository`.
- **Ports/adapters**: `MarketDataSource.list_assets` (default `[]`) + `AlpacaDataAdapter` impl
  (`get_all_assets`); `interfaces/discovery.py` (`DiscoverySource`, `DiscoveryCandidate`);
  `integrations/discovery/StockTwitsTrendingSource`.
- **Services**: `DiscoveryService.candidates_for_cycle` (merge/validate/rank/cap + persist a
  `discovery_snapshot` for the UI); `ScanCycleService` builds the effective universe
  (**pins ∪ on-demand ∪ discovered**), drains + links on-demand requests, and
  `refresh_asset_universe` (a slow `Scheduler` job).
- **UI** (`clav-web`): nav **Watchlist → Discover**; the Discover page has an "analyze a ticker"
  box (Alpaca-validated autocomplete via `/api/tickers`), a "the bot found these" board (buzz +
  price + why-link), on-demand request status, and pins management. Reuses the plain-language
  Activity/"Why?" surface.

## Safety (full auto-trade of names the operator never saw)

- **Same risk gate**: discovered/on-demand names go through `_process_symbol` → the complete risk
  engine. No path bypasses position/exposure/sector caps, daily-loss/drawdown, cooldowns, or the
  e-stop/pause veto.
- **Budget breaker**: when the Gemini daily budget is exhausted the analyst returns a neutral
  fallback → discovered names degrade to technical-only, so a market-wide hype spike can't drive a
  flurry of LLM-"confident" buys once budget is out.
- **Funnel cap**: `max_candidates_per_cycle` bounds how many new discovered positions one cycle
  can open, on top of the exposure caps.
- **Anomaly skepticism**: the analyst prompt already treats a social mention spike
  (`anomaly_flag`) as a manipulation risk and won't let it raise conviction.
- **Off by default + paper-only**: `discovery.enabled=false` out of the box; live trading is
  still Epic 6.
- **Live-money interlock**: even with discovery enabled and the two-key live gate open,
  autonomous discovery stays **off under `mode: live`** unless `sources.discovery.allow_live` is
  explicitly set. That flag is **config-only** — deliberately not a runtime/web toggle — so the
  bot can never be turned loose picking names to trade with real money from the dashboard. The
  scan cycle enforces it (`ScanCycleService._build_universe`) and the Discover page shows the
  blocked state. On-demand analysis is unaffected — it's an explicit operator action on a
  specific ticker, not autonomous name selection.

## Deferred

- **Reddit "movers" discovery source** (WSB mention tally) — the interface + funnel support a
  second source; only StockTwits trending is wired in v1.
- **Per-source discovery calibration** (which source's candidates actually pay off) — a natural
  extension of the Epic-5 calibration view, not built here.
- **A richer stock browser** (browse by sector/market-cap) — the on-demand search box covers
  "find a specific ticker"; full catalog browsing is future work.

## Verification

- Unit: `test_discovery.py` (rank/cap/dedup/validate/fail-open/snapshot), `test_asset_universe.py`
  (adapter normalization, repo search/upsert), `test_web_discover.py` (render, enqueue, dedupe,
  tradability 422, autocomplete, token).
- Integration: `test_scan_cycle_discovery.py` — a discovered name and an on-demand request both
  produce risk-gated decisions in one cycle; discovery-off leaves the universe as the watchlist.
- Full gate: ruff, mypy, import-linter, pytest.
