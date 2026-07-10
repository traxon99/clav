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
  [06 — Safety](06-safety-and-risk.md) §6.

## 5. Deployment workflow
```
git pull            # or scp release
uv pip sync         # install pinned deps into .venv
alembic upgrade head
systemctl restart clav-core clav-web
```
Wrap in `deploy/install.sh`. Keep it boring and repeatable; no manual steps.

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

## 8. Time synchronization
Enable NTP (`systemd-timesyncd`). Correct time matters for market-hours checks, candle
alignment, and cooldown windows.
