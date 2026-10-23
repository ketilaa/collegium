#!/bin/sh
# Back up the Collegium database: a custom-format dump of the database and
# the server's roles (without passwords; db/logins.sql sets those). Each dump
# is checked to be readable before older backups are pruned, so a failing
# backup never removes a good one. Dumps are compressed with zstd.
#
#   backup.sh           one backup
#   backup.sh --loop    one backup every BACKUP_INTERVAL_HOURS
#
# BACKUP_KEEP_COUNT, unset by default, also prunes to at most that many
# backups regardless of age -- the daily loop leaves it unset (age alone,
# BACKUP_KEEP_DAYS, is enough there); a deploy's one-off backup sets it to
# keep the server from accumulating one more dump every deploy forever:
#   docker compose $E run --rm --no-deps -e BACKUP_KEEP_COUNT=3 backup sh /db/backup.sh
#
# Connection from the standard PG* variables. Runs in the backup service.
set -eu

DIR=${BACKUP_DIR:-/backups}
KEEP_DAYS=${BACKUP_KEEP_DAYS:-30}
KEEP_COUNT=${BACKUP_KEEP_COUNT:-}
INTERVAL_HOURS=${BACKUP_INTERVAL_HOURS:-24}

# Every step checks its own result: the loop calls this as
# `backup_once || ...`, where the shell ignores `set -e` inside it.
backup_once() {
    rm -f "$DIR"/.partial-*
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    pg_dump --format=custom --compress=zstd:19 --file="$DIR/.partial-$stamp.dump" \
        "$PGDATABASE" || return 1
    pg_restore --list "$DIR/.partial-$stamp.dump" >/dev/null || return 1
    pg_dumpall --globals-only --no-role-passwords \
        --file="$DIR/.partial-$stamp.roles.sql" || return 1
    mv "$DIR/.partial-$stamp.dump" "$DIR/collegium-$stamp.dump" || return 1
    mv "$DIR/.partial-$stamp.roles.sql" "$DIR/collegium-$stamp.roles.sql" || return 1
    find "$DIR" -name 'collegium-*' -mtime +"$KEEP_DAYS" -delete
    if [ -n "$KEEP_COUNT" ]; then
        total=$(ls -1 "$DIR"/collegium-*.dump 2>/dev/null | wc -l)
        excess=$((total - KEEP_COUNT))
        if [ "$excess" -gt 0 ]; then
            ls -1 "$DIR"/collegium-*.dump | sort | head -n "$excess" | while IFS= read -r dump; do
                rm -f "$dump" "${dump%.dump}.roles.sql"
            done
        fi
    fi
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
