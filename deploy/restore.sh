#!/usr/bin/env bash
# Restore a backup made by `manage.py backup`.
# Usage: deploy/restore.sh /srv/flotec/backups/db-YYYYmmdd-HHMMSS.sql.gz /srv/flotec/backups/media-YYYYmmdd-HHMMSS.tar.gz
set -euo pipefail
DB_DUMP="$1"; MEDIA_TAR="${2:-}"
source /srv/flotec/app/.env
sudo systemctl stop flotec-worker flotec-web
echo "Restoring database from $DB_DUMP into $DATABASE_URL (existing data will be replaced)"
psql "$DATABASE_URL" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
gunzip -c "$DB_DUMP" | psql "$DATABASE_URL"
if [ -n "$MEDIA_TAR" ]; then
  echo "Restoring uploaded files into $MEDIA_ROOT"
  mkdir -p "$MEDIA_ROOT" && tar -xzf "$MEDIA_TAR" -C "$(dirname "$MEDIA_ROOT")"
fi
sudo systemctl start flotec-web flotec-worker
echo "Done. Automation state is as it was at backup time - check the Automation page."
