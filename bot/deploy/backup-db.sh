#!/bin/sh
# Резервная копия базы бота. Безопасна при работающем боте: используется онлайн-копия SQLite.
#
#   sudo -u plugbackup-bot /opt/plugbackup/bot/deploy/backup-db.sh
#
# База содержит Telegram id клиентов и привязку к логинам. Копии храните зашифрованными
# и не на той же машине, например кладите в своё хранилище через restic.
set -eu

DB="${DB_PATH:-/var/lib/plugbackup-bot/bot.db}"
DEST="${BACKUP_DIR:-/var/lib/plugbackup-bot/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

umask 077
mkdir -p "$DEST"
OUT="$DEST/bot-$(date +%F).db"

sqlite3 "$DB" ".backup '$OUT'"
# Копия считается годной, только если SQLite подтверждает её целостность.
if [ "$(sqlite3 "$OUT" 'PRAGMA integrity_check;')" != "ok" ]; then
    echo "backup-db: копия $OUT не прошла проверку целостности" >&2
    rm -f "$OUT"
    exit 1
fi

find "$DEST" -name 'bot-*.db' -mtime +"$KEEP_DAYS" -delete
echo "backup-db: $OUT"
