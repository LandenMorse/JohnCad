#!/usr/bin/env bash
# Restore drill: prove that a backup made by backup.sh really brings data
# back. CI runs this on every change; you can run it on a TEST install too.
#
#   scripts/restore-drill.sh
#
# Do NOT run it on a site with real data: it creates a marker account and
# file, takes a backup, deletes them, restores, and checks they came back.
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

py() { .venv/bin/python manage.py shell -c "$1" 2>/dev/null | tail -n 1; }
export BACKUP_DIR="$ROOT/backups/drill"
marker="private_media/restore-drill-marker.txt"

say "Creating marker data"
py "from django.contrib.auth import get_user_model as g; g().objects.get_or_create(username='restore-drill')"
mkdir -p private_media
echo "restore drill" > "$marker"

"$ROOT/scripts/backup.sh"
backup="$(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort | tail -n 1)"

say "Losing the data"
py "from django.contrib.auth import get_user_model as g; g().objects.filter(username='restore-drill').delete()"
rm -f "$marker"
[ "$(py "from django.contrib.auth import get_user_model as g; print(g().objects.filter(username='restore-drill').exists())")" = "False" ] \
    || die "Drill setup failed: the marker account was not deleted."

RESTORE_YES=1 "$ROOT/scripts/restore.sh" "$backup"

say "Checking the data came back"
[ "$(py "from django.contrib.auth import get_user_model as g; print(g().objects.filter(username='restore-drill').exists())")" = "True" ] \
    || die "RESTORE DRILL FAILED: the marker account is missing after restore."
[ -f "$marker" ] || die "RESTORE DRILL FAILED: the marker file is missing after restore."

# Tidy up the marker data and the drill's own backup.
py "from django.contrib.auth import get_user_model as g; g().objects.filter(username='restore-drill').delete()"
rm -f "$marker"
rm -rf "$BACKUP_DIR" "$ROOT"/private_media.before-restore-*

say "Restore drill passed."
