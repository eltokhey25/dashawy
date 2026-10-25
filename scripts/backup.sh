#!/usr/bin/env bash
# ============================================================
# Dashawy SQLite backup
#
# Usage:
#   scripts/backup.sh                 # database/fixrequest.db -> backups/
#   DB_PATH=/path/app.db BACKUP_DIR=/path/backups scripts/backup.sh
#
# The backup uses SQLite's online backup API through the `sqlite3` CLI, so it
# is safe to run while the app is live (unlike copying the file). The result is
# gzip-compressed and verified before old backups are removed.
#
# PythonAnywhere: add a Scheduled Task (daily) running
#   bash /home/<user>/<project>/scripts/backup.sh
# Keep the backups outside the web root, or download them periodically.
# ============================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_PATH="${DB_PATH:-$ROOT_DIR/database/fixrequest.db}"
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "backup: sqlite3 CLI not found." >&2
  exit 1
fi

if [ ! -f "$DB_PATH" ]; then
  echo "backup: database not found: $DB_PATH" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true

STAMP="$(date +%Y%m%d-%H%M%S)"
TMP="$BACKUP_DIR/.dashawy-$STAMP.db"
OUT="$BACKUP_DIR/dashawy-$STAMP.db.gz"

cleanup() {
  rm -f "$TMP"
}
trap cleanup EXIT

# Online, consistent backup while the app may be handling requests.
sqlite3 "$DB_PATH" ".backup '$TMP'"
sqlite3 "$TMP" "PRAGMA integrity_check;" | grep -qx 'ok' || {
  echo "backup: integrity check failed" >&2
  exit 1
}

gzip -9 -c "$TMP" > "$OUT"
chmod 600 "$OUT"

echo "backup: wrote $OUT ($(du -h "$OUT" | cut -f1))"

# Rotation: remove backups older than KEEP_DAYS. Matching only our own prefix
# avoids deleting unrelated files that happen to live in BACKUP_DIR.
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'dashawy-*.db.gz' \
  -mtime "+$KEEP_DAYS" -print -delete | sed 's/^/backup: removed /'
