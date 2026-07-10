#!/usr/bin/env bash
# deploy/install.sh — boring, repeatable deployment for clav-core on the Pi
# (docs/09-deployment.md §5). Run from the repo root as root.
#
# NOTE: written and reviewed against the docs, but not exercised on real
# Raspberry Pi hardware (this dev environment has none) — see the runbook in
# README.md for the manual verification steps to run once on the real device
# (reboot -> auto-start -> reconcile; kill -9 -> systemd restart -> reconcile).
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
rsync -a --delete \
  --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
  --exclude 'data' --exclude 'logs' --exclude '.env' --exclude 'config/config.yaml' \
  "$REPO_ROOT"/ "$CLAV_HOME"/
chown -R "$CLAV_USER:$CLAV_USER" "$CLAV_HOME"

# docs/09-deployment.md §1: DB + logs must live on the SSD, not the SD card.
# Point data_dir/log_dir (config.yaml) at the mounted SSD path, e.g.
# /mnt/ssd/clav/{data,logs}, and symlink them here — install.sh does not
# assume where your SSD is mounted, so this step is manual and intentional.
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
cp "$REPO_ROOT"/deploy/clav-backup.service /etc/systemd/system/clav-backup.service
cp "$REPO_ROOT"/deploy/clav-backup.timer /etc/systemd/system/clav-backup.timer
systemctl daemon-reload
systemctl enable --now clav-core.service
systemctl enable --now clav-backup.timer

echo "==> Done."
echo "    Status:  systemctl status clav-core"
echo "    Logs:    journalctl -u clav-core -f"
echo "    Control: sudo -u $CLAV_USER $CLAV_HOME/.venv/bin/clav-ctl status"
