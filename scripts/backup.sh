#!/bin/sh
# Nightly backup of the GATS data directory on the Linux VM (T7.4).
#
#   scripts/backup.sh /opt/gats/app/data /mnt/backup/gats
#
# The raw store is append-only and content-addressed, so rsync copies only
# new files. The SQLite database is copied through its own online-backup
# command: copying the file while the recorder writes could tear it.
# Nothing under the data directory is ever deleted or changed by this script.
set -eu

DATA="${1:?usage: backup.sh <data dir> <backup dir>}"
DEST="${2:?usage: backup.sh <data dir> <backup dir>}"
STAMP="$(date -u +%Y%m%d)"

mkdir -p "$DEST/raw" "$DEST/bars" "$DEST/db"

# 1. Raw payloads and bar files: immutable once written.
rsync -a --ignore-existing "$DATA/raw/" "$DEST/raw/"
if [ -d "$DATA/bars" ]; then
    rsync -a "$DATA/bars/" "$DEST/bars/"
fi

# 2. The database, as a consistent snapshot (keeps the last 14).
if [ -f "$DATA/gats.db" ]; then
    sqlite3 "$DATA/gats.db" ".backup '$DEST/db/gats-$STAMP.db'"
    gzip -f "$DEST/db/gats-$STAMP.db"
    ls -1t "$DEST/db"/gats-*.db.gz | tail -n +15 | xargs -r rm --
fi

echo "$(date -u +%FT%TZ) backup of $DATA to $DEST done"
