#!/bin/sh
# Back up the Collegium database: a custom-format dump of the database and
# the server's roles (without passwords; db/logins.sql sets those). Each dump
# is checked to be readable before older backups are pruned, so a failing
# backup never removes a good one.
#
#   backup.sh           one backup
#   backup.sh --loop    one backup every BACKUP_INTERVAL_HOURS
#
# Connection from the standard PG* variables. Runs in the backup service.
set -eu

DIR=${BACKUP_DIR:-/backups}
KEEP_DAYS=${BACKUP_KEEP_DAYS:-30}
INTERVAL_HOURS=${BACKUP_INTERVAL_HOURS:-24}

backup_once() {
    rm -f "$DIR"/.partial-*
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    pg_dump --format=custom --file="$DIR/.partial-$stamp.dump" "$PGDATABASE"
    pg_restore --list "$DIR/.partial-$stamp.dump" >/dev/null
    pg_dumpall --globals-only --no-role-passwords --file="$DIR/.partial-$stamp.roles.sql"
    mv "$DIR/.partial-$stamp.dump" "$DIR/collegium-$stamp.dump"
    mv "$DIR/.partial-$stamp.roles.sql" "$DIR/collegium-$stamp.roles.sql"
    find "$DIR" -name 'collegium-*' -mtime +"$KEEP_DAYS" -delete
    echo "backup collegium-$stamp: $(du -h "$DIR/collegium-$stamp.dump" | cut -f1)"
}

mkdir -p "$DIR"
if [ "${1:-}" = "--loop" ]; then
    while true; do
        backup_once || echo "backup failed; keeping earlier backups" >&2
        sleep $((INTERVAL_HOURS * 3600))
    done
else
    backup_once
fi
