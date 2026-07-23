# 15 — Go-Live Checklist

Story 6.6 (`docs/epics/epic-06-live-trading-and-soak.md`, decision #6). The exit from paper to
live capital is a **human sign-off**, not automation — CLAV never self-promotes. This checklist
is what that sign-off means in practice: read the soak report, confirm the pilot profile and the
live-key separation, then flip the two-key gate.

Work through it top to bottom. If any step fails, stop — fix the underlying issue, let the soak
continue, and come back to the checklist. Nothing here is a formality to skim past; every item
maps to a specific failure mode a live-money system can't afford.

## 1. Run a clean multi-day paper soak

Run `clav-core`/`clav-web` on `mode: paper` for **at least several trading days**, uninterrupted,
against the config you intend to go live with (watchlist, risk knobs, LLM settings) — not a
one-off smoke test. The soak is what the report in step 2 summarizes; a short or interrupted soak
just produces a report with too little data to mean anything.

## 2. Read the soak report

```
clav-ctl soak-report --hours 72   # or --start/--end to cover the exact soak window
```

Read every line, not just the final verdict:

- **`duplicate client_order_ids`** — must be **0**. This should always be zero (the DB itself
  enforces it — see `docs/06-safety-and-risk.md` and the Epic-1 safety-invariant suite); the
  report re-checks it independently. Any non-zero count means something is deeply wrong upstream
  and go-live is off the table until it's understood.
- **`failed orders` / `stuck cycles` / `unhandled errors`** — must be **0**. A failed order or a
  cycle that started and never finished is exactly the kind of silent failure the soak exists to
  surface before it happens with real money.
- **`critical health events`** — investigate any nonzero count even if it's since resolved (a
  broker outage, a Gemini breaker trip, a daily-loss warning). Know *why* each one happened.
  Isolated, understood, and since-recovered events are a normal part of a healthy soak; a pattern
  of them is not.
- **`liveness`** — must read `ok`. `degraded`/`down` means the process wasn't reliably alive
  through the window, which the soak is specifically meant to prove.
- **`daily P&L` / headroom** — sanity-check that the daily-loss cap actually bound realistic
  moves during the soak; a headroom that's suspiciously large or small compared to what you
  expect may mean the config's `max_daily_loss_pct` isn't set where you think it is.

The final line reads either `CLEAN — ready for go-live checklist review` or
`NOT CLEAN — resolve the findings above before the go-live checklist`. Do not proceed on a
`NOT CLEAN` report — resolve every finding, then re-run the soak (or at minimum re-run the report
over a window that no longer includes the resolved issue) until it reads `CLEAN`.

## 3. Confirm the pilot profile

Review `config/config.pilot.example.yaml` line by line against your own risk tolerance before
copying it to `config/config.yaml`:

- `max_position_value` / `max_daily_loss_pct` / `max_drawdown_pct` are all tightened well below
  `config.example.yaml`'s paper defaults (epic-06 decision #5) — confirm the tightened numbers,
  not the paper ones, are what you actually want risked first.
- `watchlist` is short (two liquid, well-covered names) — deliberately small blast radius for a
  first pilot, not the full paper watchlist.
- `flatten_on_estop: true` is set — an emergency stop during the pilot force-closes open
  positions (Story 6.3) rather than merely freezing new entries. Confirm this is the behavior you
  want for real money before relying on it.
- No new limit mechanism exists here — every cap is an existing Epic-2 risk knob, just sized
  down. If a number in this file doesn't look tight enough, edit it; there's no separate "pilot
  mode" in the code enforcing anything beyond what's in this YAML.

## 4. Confirm live keys are separate from paper

- `CLAV_ALPACA_LIVE__API_KEY` / `CLAV_ALPACA_LIVE__API_SECRET` in `.env` are a **different**
  Alpaca key pair from `CLAV_ALPACA__API_KEY` / `CLAV_ALPACA__API_SECRET` (paper) — never the
  same credentials reused. This is what makes it structurally impossible for a paper key to
  authenticate a live session (`docs/06-safety-and-risk.md` §6, epic-06 decision #1).
- Confirm the live key pair belongs to the live Alpaca account you intend to trade, funded with
  only the capital you're willing to risk in the pilot.
- Confirm `.env` is `600`-permissioned and was never committed (`docs/09-deployment.md` §4).

## 5. Sign off

Once steps 1–4 all pass: record who reviewed the soak report and pilot profile, and when. A line
in your own operational log is enough — CLAV doesn't require a specific tool for this, only that
a human, not the bot, made the call.

## 6. Flip the gate

Only after sign-off:

1. Copy `config/config.pilot.example.yaml` to `config/config.yaml` (or merge its values into
   your existing config) — this sets `mode: live` and `i_understand_live_trading: true` together.
2. Restart `clav-core`/`clav-web` (`docs/09-deployment.md` §5).
3. Confirm the **LIVE** banner appears on every dashboard page and `GET /health` reports
   `mode: live` (Story 6.4) — this is the last visual confirmation before the system can place a
   real order.

Missing *any* of `mode: live`, `i_understand_live_trading: true`, or the separate live credentials
means the process refuses to start rather than silently falling back to paper or trading
uncapped — see the fail-closed matrix in `docs/06-safety-and-risk.md` §7.

## Rolling back to paper

Set `mode: paper` (and drop `i_understand_live_trading`, though it's inert outside `mode: live`)
and restart. The live broker is never constructed outside the gate, so this is a clean, immediate
return to paper — no separate teardown step.

## See also

- `docs/06-safety-and-risk.md` §6–7 — the live-trading gate and fail-closed matrix this checklist
  gates access to.
- `docs/09-deployment.md` §4 — secrets and config location on the deployment host.
- `docs/epics/epic-06-live-trading-and-soak.md` — the full epic this checklist is Story 6.6 of.
