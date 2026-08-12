#!/usr/bin/env bash
# deploy/install.sh — boring, repeatable deployment for clav-core on the Pi
# (docs/09-deployment.md §5). Run from the repo root as root.
#
# Verified end-to-end on real Raspberry Pi hardware (Pi 4, Raspberry Pi OS /
# Debian 13) — see docs/09-deployment.md §9 for the live verification notes
# (fresh install, .env/config.yaml provided post-install, both services
# reached steady state, desktop launcher opened the dashboard correctly).
#
# Usage: sudo ./deploy/install.sh
set -euo pipefail

CLAV_USER="${CLAV_USER:-clav}"
CLAV_HOME="${CLAV_HOME:-/opt/clav}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo ./deploy/install.sh" >&2
  exit 1
fi

if ! id "$CLAV_USER" &>/dev/null; then
  echo "==> Creating system user $CLAV_USER"
  useradd --system --home "$CLAV_HOME" --create-home --shell /usr/sbin/nologin "$CLAV_USER"
fi

echo "==> Syncing repo to $CLAV_HOME"
mkdir -p "$CLAV_HOME"
# $CLAV_HOME doubles as both the sync destination and $CLAV_USER's $HOME
# (useradd --home above), so this must also exclude uv's own state dirs
# (.cache, .local) -- they live under $HOME by convention but aren't part of
# the repo. Without these, --delete tries to prune them to match the repo
# tree and can corrupt an in-progress Python/venv install out from under uv.
rsync -a --delete \
  --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
  --exclude '.cache' --exclude '.local' \
  --exclude '/data' --exclude '/logs' --exclude '.env' --exclude 'config/config.yaml' \
  "$REPO_ROOT"/ "$CLAV_HOME"/
chown -R "$CLAV_USER:$CLAV_USER" "$CLAV_HOME"

# docs/09-deployment.md §1: DB + logs must live on the SSD, not the SD card,
# if you have one. On an SD-card-only Pi there's nowhere else for them to
# go; install.sh doesn't assume either way, so pointing data_dir/log_dir at
# a mounted SSD and symlinking them here is a manual, optional step.
mkdir -p "$CLAV_HOME"/data "$CLAV_HOME"/logs "$CLAV_HOME"/backups
chown -R "$CLAV_USER:$CLAV_USER" "$CLAV_HOME"/data "$CLAV_HOME"/logs "$CLAV_HOME"/backups

if [[ ! -f "$CLAV_HOME/.env" ]]; then
  echo "!! $CLAV_HOME/.env is missing." >&2
  echo "!! Copy .env.example there and fill in real Alpaca *paper* keys before starting the service." >&2
fi
if [[ ! -f "$CLAV_HOME/config/config.yaml" ]]; then
  echo "!! $CLAV_HOME/config/config.yaml is missing." >&2
  echo "!! Copy config/config.example.yaml there and edit the watchlist/schedule." >&2
fi

echo "==> Installing uv (if needed) and syncing dependencies as $CLAV_USER"
sudo -u "$CLAV_USER" bash -c '
  set -euo pipefail
  command -v uv >/dev/null 2>&1 || { curl -LsSf https://astral.sh/uv/install.sh | sh; }
  export PATH="$HOME/.local/bin:$PATH"
  cd '"$CLAV_HOME"'
  uv sync --frozen --no-group dev
'

echo "==> Running Alembic migrations"
sudo -u "$CLAV_USER" bash -c '
  export PATH="$HOME/.local/bin:$PATH"
  cd '"$CLAV_HOME"'
  uv run alembic upgrade head
'

echo "==> Installing systemd units"
cp "$REPO_ROOT"/deploy/clav-core.service /etc/systemd/system/clav-core.service
cp "$REPO_ROOT"/deploy/clav-web.service /etc/systemd/system/clav-web.service
cp "$REPO_ROOT"/deploy/clav-backup.service /etc/systemd/system/clav-backup.service
cp "$REPO_ROOT"/deploy/clav-backup.timer /etc/systemd/system/clav-backup.timer
systemctl daemon-reload
systemctl enable clav-core.service clav-web.service clav-backup.timer
systemctl start clav-backup.timer
# `enable --now` would abort this whole script (set -e) if .env/config.yaml
# aren't in place yet -- a very ordinary first-install state, not a fatal
# error. Both units are enabled either way (start on every future boot);
# Restart=on-failure means providing the missing file and re-running
# `systemctl restart` (or just rebooting) is all that's needed afterward.
systemctl restart clav-core.service ||
  echo "!! clav-core didn't start -- almost certainly the missing .env/config.yaml warned about above. It's enabled for boot; run 'sudo systemctl restart clav-core' once those exist." >&2
systemctl restart clav-web.service ||
  echo "!! clav-web didn't start -- same cause as clav-core above. 'sudo systemctl restart clav-web' once fixed." >&2

echo "==> Installing the desktop launcher"
REAL_USER="${SUDO_USER:-$(logname 2>/dev/null || true)}"
if [[ -n "$REAL_USER" ]] && id "$REAL_USER" &>/dev/null; then
  USER_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
  for DEST in "$USER_HOME/Desktop" "$USER_HOME/.local/share/applications"; do
    install -d -o "$REAL_USER" -g "$REAL_USER" "$DEST"
    sed "s#__USER_HOME__#$USER_HOME#" "$REPO_ROOT/deploy/clav-dashboard.desktop" >"$DEST/clav-dashboard.desktop"
    chown "$REAL_USER:$REAL_USER" "$DEST/clav-dashboard.desktop"
    chmod 755 "$DEST/clav-dashboard.desktop"
  done
  # The Desktop copy gets double-clicked from the file manager, which
  # otherwise shows an "Execute File?" confirmation every time until this
  # metadata bit is set (PCManFM/Raspberry Pi OS behavior for .desktop files
  # sitting directly on the Desktop -- the applications-menu copy doesn't
  # need it). --password-store=basic in the launcher's Exec line avoids a
  # separate first-launch snag: Chromium otherwise prompts to unlock/create
  # a GNOME keyring the first time it runs.
  if command -v gio >/dev/null 2>&1; then
    sudo -u "$REAL_USER" gio set "$USER_HOME/Desktop/clav-dashboard.desktop" metadata::trusted true || true
  fi
else
  echo "!! Could not determine the desktop user (not invoked via sudo from a login session) -- skipped the desktop launcher." >&2
  echo "!! Copy deploy/clav-dashboard.desktop to ~/Desktop yourself, replacing __USER_HOME__ with your actual home directory." >&2
fi

echo "==> Done."
echo "    Status:      systemctl status clav-core clav-web"
echo "    Logs:        journalctl -u clav-core -f"
echo "    Control:     sudo -u $CLAV_USER $CLAV_HOME/.venv/bin/clav-ctl status"
echo "    Dashboard:   the new 'CLAV Dashboard' desktop icon, or"
echo "                 http://localhost:8080 on the Pi itself (binds to 127.0.0.1 by"
echo "                 default -- set web.bind_host: 0.0.0.0 in config.yaml, or your"
echo "                 Tailscale IP, to reach it from another device on the LAN)"
echo "    Real history: deploy/backfill_portfolio_history.py seeds the dashboard's"
echo "                 equity chart from Alpaca's own account history -- run it once"
echo "                 (as $CLAV_USER, from $CLAV_HOME) if reusing keys with prior activity."
