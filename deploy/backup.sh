#!/usr/bin/env bash
# Nightly SQLite backup. Uses SQLite's online backup API through the app
# container so the copy is consistent even while the app is writing.
#
#   crontab -e
#   15 4 * * * /home/cloudset/cloudset/deploy/backup.sh >> /home/cloudset/backups/backup.log 2>&1
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${CLOUDSET_BACKUP_DIR:-$HOME/backups}"
KEEP_DAYS="${CLOUDSET_BACKUP_KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"
cd "$REPO_DIR"

docker compose exec -T app python - <<'PY'
import sqlite3
src = sqlite3.connect("/app/data/cloudset.db")
dst = sqlite3.connect("/app/data/backup.tmp.db")
src.backup(dst)
dst.close()
src.close()
PY

mv "$REPO_DIR/data/backup.tmp.db" "$BACKUP_DIR/cloudset-$STAMP.db"
gzip -f "$BACKUP_DIR/cloudset-$STAMP.db"
find "$BACKUP_DIR" -name 'cloudset-*.db.gz' -mtime +"$KEEP_DAYS" -delete
echo "$(date -Is) backed up to $BACKUP_DIR/cloudset-$STAMP.db.gz"
