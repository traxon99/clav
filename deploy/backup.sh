#!/usr/bin/env bash
# deploy/backup.sh — nightly `VACUUM INTO` a timestamped copy of the live
# WAL-mode database, safe to run while clav-core is up (docs/09-deployment.md
# §6). Invoked by clav-backup.timer; can also be run manually.
#
# NOTE: not exercised against a real deployed instance in this dev
# environment (no sqlite3 CLI / no Pi here) — verify once for real before
# relying on it, per the runbook in README.md.
set -euo pipefail

CLAV_HOME="${CLAV_HOME:-/opt/clav}"
DB_PATH="${CLAV_DB_PATH:-$CLAV_HOME/data/clav.db}"
BACKUP_DIR="${CLAV_BACKUP_DIR:-$CLAV_HOME/backups}"
KEEP_DAYS="${CLAV_BACKUP_KEEP_DAYS:-14}"

if [[ ! -f "$DB_PATH" ]]; then
  echo "No database at $DB_PATH yet; nothing to back up." >&2
  exit 0
fi

mkdir -p "$BACKUP_DIR"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
dest="$BACKUP_DIR/clav-$timestamp.db"

sqlite3 "$DB_PATH" "VACUUM INTO '$dest';"
echo "Backed up $DB_PATH -> $dest"

find "$BACKUP_DIR" -name 'clav-*.db' -mtime "+$KEEP_DAYS" -delete
