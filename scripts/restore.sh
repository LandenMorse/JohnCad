#!/usr/bin/env bash
# Restore the database and uploaded files from a backup made by backup.sh.
#
#   scripts/restore.sh backups/20261112-023000
#
# This REPLACES the current database and uploads with the backup's contents.
# The uploads that are there now are moved aside to private_media.before-restore-<time>,
# not deleted. Details: docs/operations.md.
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

src="${1:-}"
[ -n "$src" ] || die "Usage: scripts/restore.sh <backup directory>"
src="$(cd "$src" 2>/dev/null && pwd)" || die "No such backup directory: $1"
[ -s "$src/db.dump" ] || die "$src has no db.dump; not a JohnCad backup."
[ -f "$src/media.tar" ] || die "$src has no media.tar; not a JohnCad backup."

echo "About to replace the JohnCad database and uploads with the backup in:"
echo "  $src"
if [ "${RESTORE_YES:-}" != "1" ]; then
    read -r -p "Type 'restore' to continue: " answer
    [ "$answer" = "restore" ] || die "Cancelled; nothing was changed."
fi

have_services=0
if command -v systemctl >/dev/null 2>&1 && systemctl cat johncad.service >/dev/null 2>&1; then
    have_services=1
    say "Stopping JohnCad"
    sudo systemctl stop "${SERVICES[@]}"
fi

say "Restoring the database"
dc up -d db
wait_for_db
dc exec -T db sh -c 'pg_restore --clean --if-exists --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$src/db.dump"

say "Restoring uploaded files"
if [ -d "$ROOT/private_media" ]; then
    mv "$ROOT/private_media" "$ROOT/private_media.before-restore-$(date +%Y%m%d-%H%M%S)"
fi
tar -C "$ROOT" -xf "$src/media.tar"

if [ "$have_services" = 1 ]; then
    say "Starting JohnCad"
    sudo systemctl start "${SERVICES[@]}"
    site_healthy || die "The site did not come back. Look at: journalctl -u johncad -n 50"
fi

say "Restore finished."
if [ -f "$src/git-commit.txt" ]; then
    echo "The backup was taken at code version $(cat "$src/git-commit.txt")."
    echo "If the site misbehaves, check that version out: see docs/operations.md, 'Rolling back'."
fi
