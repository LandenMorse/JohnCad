#!/usr/bin/env bash
# Back up the JohnCad database and uploaded files.
#
#   scripts/backup.sh
#
# Writes backups/<timestamp>/ containing:
#   db.dump     Postgres dump (pg_dump custom format)
#   media.tar   everything under private_media/ (uploads and sliced files)
#
# Settings (environment variables, or put them in .env):
#   BACKUP_DIR   where backups go. Default: <project>/backups. Point this at
#                a USB drive or network share: a backup on the same SD card
#                does not survive that card failing.
#   BACKUP_KEEP  how many backups to keep. Default: 14.
#
# Restore with scripts/restore.sh. Details: docs/operations.md.
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# Pick up BACKUP_DIR / BACKUP_KEEP from .env if they are set there.
if [ -f "$ROOT/.env" ]; then
    for name in BACKUP_DIR BACKUP_KEEP; do
        if [ -z "${!name:-}" ]; then
            value="$(grep -E "^${name}=" "$ROOT/.env" | tail -n 1 | cut -d= -f2- || true)"
            [ -n "$value" ] && export "$name=$value"
        fi
    done
fi
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
BACKUP_KEEP="${BACKUP_KEEP:-14}"

stamp="$(date +%Y%m%d-%H%M%S)"
dest="$BACKUP_DIR/$stamp"
tmp="$BACKUP_DIR/.incomplete-$stamp"
mkdir -p "$tmp"
chmod 700 "$BACKUP_DIR" "$tmp"
trap 'rm -rf "$tmp"' EXIT

say "Dumping the database"
dc exec -T db sh -c 'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$tmp/db.dump"
[ -s "$tmp/db.dump" ] || die "The database dump is empty; backup aborted."

say "Archiving uploaded files"
mkdir -p "$ROOT/private_media"
tar -C "$ROOT" --exclude='private_media/slicing_tmp' -cf "$tmp/media.tar" private_media

git -C "$ROOT" rev-parse HEAD > "$tmp/git-commit.txt" 2>/dev/null || true

# Only a finished backup gets its real name, so a half-written one is never
# mistaken for a good one.
mv "$tmp" "$dest"
trap - EXIT

# Drop the oldest backups beyond BACKUP_KEEP.
mapfile -t all < <(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort)
excess=$(( ${#all[@]} - BACKUP_KEEP ))
if [ "$excess" -gt 0 ]; then
    for old in "${all[@]:0:$excess}"; do
        rm -rf "$old"
    done
fi

say "Backup written to $dest ($(du -sh "$dest" | cut -f1))"
