#!/usr/bin/env bash
# Update a running JohnCad to the latest version.
#
#   scripts/update.sh
#
# Takes a backup, pulls the new code, installs dependencies, applies database
# changes, restarts the services and checks the site answers. Safe to run
# again if it stops part-way. Details and rollback: docs/operations.md.
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

[ -x .venv/bin/python ] || die "No .venv here. Run scripts/setup-site.sh first."

previous="$(git rev-parse HEAD)"

say "Backing up first"
"$ROOT/scripts/backup.sh"

say "Getting the new version"
git pull --ff-only

say "Installing dependencies"
.venv/bin/pip install --quiet -r requirements.txt

say "Applying database changes"
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py collectstatic --noinput >/dev/null

say "Restarting JohnCad"
sudo systemctl restart "${SERVICES[@]}"

if site_healthy; then
    say "Updated: $(git rev-parse --short "$previous") -> $(git rev-parse --short HEAD). The site is up."
else
    warn "The site is not answering after the update."
    echo "See what went wrong:   journalctl -u johncad -n 50"
    echo "Go back to the previous version:"
    echo "  git checkout $previous"
    echo "  .venv/bin/pip install -r requirements.txt"
    echo "  sudo systemctl restart ${SERVICES[*]}"
    echo "More: docs/operations.md, 'Rolling back'."
    exit 1
fi
