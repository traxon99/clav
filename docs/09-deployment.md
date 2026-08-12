# 09 — Deployment on Raspberry Pi 4 (2 GB)

## 1. OS & runtime baseline
- Linux Mint (Debian/Ubuntu base) — apply updates, enable `unattended-upgrades` for security
  patches only.
- Python 3.11+ via `pyenv` or system Python; project in a **virtualenv** managed by `uv` or
  `pip`.
- **Move the DB and logs off the microSD card** onto a USB SSD/flash drive. SD cards wear out
  and corrupt under sustained writes; WAL + logs write constantly. Mount the SSD and point
  `data_dir`/`log_dir` there.
- Enable a **swap file** (e.g. 1–2 GB `zram` or a swapfile on the SSD) as a safety cushion —
  but treat swapping as a warning sign, not normal operation.
- Set timezone to UTC on the host; the dashboard localizes for display.

## 2. Resource budget (rough, 2 GB total)

| Component | Target RSS |
|-----------|-----------|
| `clav-core` (Python + pandas/numpy) | 150–350 MB |
| `clav-web` (FastAPI/uvicorn, 1 worker) | 80–150 MB |
| SQLite (in-process, cache) | tens of MB |
| OS + services | ~300–500 MB |
| Headroom / swap cushion | remainder |

`HealthMonitor` samples process RSS and system memory each cycle; a memory-pressure event
raises a warning and can pause new analysis (the LLM path allocates the most).

## 3. Process supervision (systemd)

Two units, each `Restart=on-failure`, journald logging, and a memory guard.

```ini
# deploy/clav-core.service
[Unit]
Description=CLAV core trading service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=clav
WorkingDirectory=/opt/clav
EnvironmentFile=/opt/clav/.env
ExecStart=/opt/clav/.venv/bin/python -m clav.app core
Restart=on-failure
RestartSec=10
MemoryMax=450M          # cgroup cap; OOM-kill core before it starves the Pi
Nice=5

[Install]
WantedBy=multi-user.target
```

```ini
# deploy/clav-web.service  (similar; MemoryMax=200M, ExecStart ... app web)
```

- `MemoryMax` makes the kernel kill and restart a runaway process instead of hanging the
  whole board. On restart, `clav-core` **reconciles with the broker before trading**, so a
  restart mid-cycle is safe.
- Use `systemctl enable` so both start on boot → always-on server.

## 4. Configuration & secrets
- Real secrets live in `/opt/clav/.env` (mode `600`, owned by `clav`), referenced by name in
  `config.yaml`. Never commit secrets; `.env.example` documents the keys only.
- `mode: paper` is the shipped default. Live requires the explicit gate in
  [06 — Safety](06-safety-and-risk.md) §6, and only after working through the
  [15 — Go-Live Checklist](15-go-live-checklist.md) (clean soak report, pilot profile reviewed,
  live keys confirmed separate from paper).

## 5. Deployment workflow
```
git pull            # or scp release
uv pip sync         # install pinned deps into .venv
alembic upgrade head
systemctl restart clav-core clav-web
```
`deploy/install.sh` wraps all of this — system dependencies, the venv, migrations, both
systemd units (enabled for boot even if `.env`/`config.yaml` aren't in place yet, so a
first run never has to be re-invoked once you provide them), and the desktop launcher
(§7). `sudo ./deploy/install.sh` end to end; safe to re-run after a `git pull` to pick up
updates. Verified on real Raspberry Pi hardware, not just reviewed against these docs.

## 6. Backups & durability
- **Nightly backup job** (`deploy/backup.sh`): `VACUUM INTO` a timestamped copy on the SSD,
  then optionally push (encrypted) off-device. `VACUUM INTO` is safe on a live WAL DB.
- Retain N daily + M weekly snapshots; prune the rest.
- The DB is the journal — losing it loses the whole point. Test a restore periodically.

## 7. Networking & access
- Dashboard bound to `127.0.0.1` (or LAN only). For remote access use **Tailscale/WireGuard
  or an SSH tunnel** — never expose the dashboard or any trading control to the public
  internet. See [10 — Observability](10-observability.md) and security notes in
  [06 — Safety](06-safety-and-risk.md).
- Outbound only to Alpaca, Gemini, and news hosts.

### On-device access: the desktop launcher

`deploy/install.sh` installs a **CLAV Dashboard** launcher to both `~/Desktop` and the
applications menu (`deploy/clav-dashboard.desktop`) for whichever user invoked `sudo` —
a Chromium *app-mode* window (`--app=http://127.0.0.1:8080`, no address bar/tabs) rather
than a browser tab, so it looks and feels like a native app over Pi Connect or a directly
attached monitor. Two Chromium flags matter and are baked into the launcher, not optional:
- `--password-store=basic` — without it, Chromium's first launch prompts to unlock/create a
  GNOME keyring before the window ever renders (a generic Chromium↔keyring integration
  snag, unrelated to CLAV itself, confirmed live on Raspberry Pi OS/PCManFM).
- `--user-data-dir=<home>/.config/clav-dashboard-chromium` — an isolated profile, so this
  doesn't share cookies/history with the user's regular browsing.

The install script also marks the Desktop copy `gio ... metadata::trusted true` — otherwise
PCManFM shows an "Execute File?" confirmation on every double-click of a `.desktop` file
sitting directly on the Desktop (the applications-menu copy isn't subject to this check).

If `install.sh` can't determine the invoking desktop user (not run via `sudo` from a login
session), it skips this step and says so; copy `deploy/clav-dashboard.desktop` to `~/Desktop`
by hand, replacing `__USER_HOME__` with the real home directory.

## 8. Time synchronization
Enable NTP (`systemd-timesyncd`). Correct time matters for market-hours checks, candle
alignment, and cooldown windows.

## 9. First run: what to expect

**The dashboard starts empty, and that's normal.** `clav-web` only ever displays what
`clav-core` has itself persisted (`web/portfolio_value.py` is explicitly "no new capture
plumbing" — it never queries Alpaca's own account history). Two things gate the first real
data:

- **The first scan cycle isn't immediate.** `scan_interval_minutes` (default 30) is an
  `APScheduler` `IntervalTrigger` with no explicit start time, so the first fire is ~30
  minutes after `clav-core` starts, not on startup.
- **Scan cycles are gated on the real market clock**, not just `trading_window` in
  `config.yaml` — outside regular NYSE/NASDAQ hours, a cycle logs `scan_cycle_skipped_market_closed`
  and does nothing (no watchlist prices, no portfolio snapshot), even if the 30-minute
  timer fires. An on-demand "analyze this ticker now" request (Discover page) is the one
  exception — it still runs Gemini's read overnight, it just can't open a new position
  until the market's open (`TradingHoursRule` vetoes the BUY, the decision itself is still
  journaled).

So an install started while the market is closed will show an empty chart and "no price yet" watchlist
cards until the next in-hours cycle — expected, not broken. Confirm the scheduler is
actually alive via `journalctl -u clav-core -f` (look for `scheduler_started`, then
eventually `scan_cycle_skipped_market_closed` or a completed cycle) rather than trusting
the dashboard alone during this window.

**Reusing Alpaca paper keys with prior activity?** The account already has real equity
history that a fresh `clav.db` has no record of — `deploy/backfill_portfolio_history.py`
imports it once via Alpaca's own `get_portfolio_history` API (`TradingClient` method,
already a dependency — no new one added) directly into `portfolio_snapshot`, computing
`peak_equity`/`drawdown` as a running max/drawdown over the imported series so the live
system's *next* snapshot continues from the real all-time high, not zero. Idempotent
(skips timestamps that already have a row) — safe to re-run, e.g. after a real outage gap.

```bash
sudo -u clav bash -c 'cd /opt/clav && .venv/bin/python deploy/backfill_portfolio_history.py'
```

Same idea for the watchlist's own price history (`deploy/backfill_watchlist_candles.py`) — populates
the Home page's per-ticker sparklines and gives the technical-indicator pipeline (ATR, moving
averages, ...) real lookback for the very first cycle, instead of it building up one bar at a
time from a cold start:

```bash
sudo -u clav bash -c 'cd /opt/clav && .venv/bin/python deploy/backfill_watchlist_candles.py'
```

**Two real bugs found while building the above, not just first-run gaps** — both in
`AlpacaDataAdapter._fetch_candles` (`clav/integrations/alpaca_data.py`), confirmed live
against a real account and now fixed, but worth understanding since they affected every
regular scan cycle's candle fetch too, not just backfills:

1. The request used `start` + `limit` with Alpaca's default sort (ascending) — which returns
   the *oldest* `limit` bars counting forward from `start`, not the most recent ones. With
   the ~330-calendar-day start buffer `_lookback_start` computes (comfortably more than
   `limit` trading days fit in that span), every fetch silently stopped about 5 weeks short
   of "now" — no error, just quietly stale data feeding every indicator. Fixed with
   `sort=Sort.DESC` (most recent bars first), reversed back to the oldest-first order
   `get_candles`'s own contract promises.
2. Alpaca's default feed (SIP, the consolidated tape) rejects querying recent data on the
   free market-data plan this project targets — `"subscription does not permit querying
   recent SIP data"`. This only started mattering once (1) was fixed and requests actually
   reached into recent dates. Fixed with `feed=DataFeed.IEX`, the free tier's real feed.
