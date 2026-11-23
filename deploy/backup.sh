#!/usr/bin/env bash
#
# Back up the SQLite database.
#
# Uses SQLite's own `.backup`, which takes a consistent snapshot of a live
# database - copying the file with cp while the worker is mid-write can capture
# a torn page and, in WAL mode, miss committed data still in the -wal file.
#
# Usage:
#     sudo bash /opt/cryptoviz/deploy/backup.sh [destination-dir]
#
# Install as a daily cron job:
#     echo '0 3 * * * root bash /opt/cryptoviz/deploy/backup.sh' \
#         > /etc/cron.d/cryptoviz-backup

set -euo pipefail

ENV_FILE="/opt/cryptoviz/.env"

# Use the same database the services use, as configured in their .env file.
if [[ -z "${CRYPTOVIZ_DB_PATH:-}" && -r "$ENV_FILE" ]]; then
    CRYPTOVIZ_DB_PATH="$(sed -n 's/^CRYPTOVIZ_DB_PATH=//p' "$ENV_FILE" | tail -n 1)"
fi

DB_PATH="${CRYPTOVIZ_DB_PATH:-/var/lib/cryptoviz/crypto.db}"
DEST_DIR="${1:-/var/backups/cryptoviz}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

if [[ ! -f "$DB_PATH" ]]; then
    echo "Database not found at $DB_PATH" >&2
    exit 1
fi

mkdir -p "$DEST_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$DEST_DIR/crypto-$STAMP.db"

# Never leave a partial, uncompressed file behind for rotation to miss.
trap 'rm -f "$DEST"' EXIT

sqlite3 "$DB_PATH" ".backup '$DEST'"

if [[ "$(sqlite3 "$DEST" 'PRAGMA integrity_check;')" != "ok" ]]; then
    echo "Backup failed its integrity check; discarding it" >&2
    exit 1
fi

gzip -f "$DEST"

echo "Backed up to $DEST.gz"

# Drop backups older than the retention window.
find "$DEST_DIR" -name 'crypto-*.db.gz' -mtime "+$RETENTION_DAYS" -delete

echo "Retained backups:"
ls -1sh "$DEST_DIR" | tail -n +2
