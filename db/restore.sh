#!/bin/sh
# Restore a backup into a NEW database, never over the live one:
#
#   restore.sh /backups/collegium-<stamp>.dump [target]    (default target: collegium_restored)
#
# Roles must exist first. On a fresh server, apply the matching roles file:
#   psql -f /backups/collegium-<stamp>.roles.sql   then   db/logins.sql
# Check the restored database, then switch to it by renaming databases.
set -eu

DUMP=$1
TARGET=${2:-collegium_restored}
[ "$TARGET" != "${PGDATABASE:-collegium}" ] || { echo "refusing to restore over $TARGET" >&2; exit 1; }

createdb "$TARGET"
pg_restore --dbname="$TARGET" --exit-on-error "$DUMP"
echo "restored $DUMP into $TARGET"
