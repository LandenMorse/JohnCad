#!/usr/bin/env bash
# One-command install of the JohnCad SITE on a Raspberry Pi (Raspberry Pi OS
# 64-bit or another Debian/Ubuntu system). This is the Pi that runs the
# website, the database and the slicer. The printers' side (Bambuddy + print
# agent, usually a second Pi at school) is set up separately: agent/README.md.
#
#   git clone https://github.com/LandenMorse/JohnCad.git && cd JohnCad
#   scripts/setup-site.sh
#
# What it does, in order:
#   1. installs system packages (Docker, Python venv, Flatpak, OrcaSlicer)
#   2. creates the Python environment in .venv/
#   3. writes .env with freshly generated secrets, set up for a print agent
#      (never overwrites an existing one)
#   4. starts Postgres and creates the database tables
#   5. installs and starts the site and slicer services, plus a nightly backup
#   6. asks you to create the admin login
#
# It is safe to run again: every step checks what is already done.
#
# Options:
#   --no-orca   don't install the OrcaSlicer Flatpak (large download)
#
# After it finishes the site answers on this Pi at http://127.0.0.1:8000/.
# A public address, the print agent and slicer presets are separate steps;
# the script prints what is left. Full guide: README.md, docs/operations.md.
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

install_orca=1
for arg in "$@"; do
    case "$arg" in
        --no-orca) install_orca=0 ;;
        -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *) die "Unknown option: $arg (try --help)" ;;
    esac
done

[ "$(id -u)" -ne 0 ] || die "Run this as your normal user, not with sudo. It asks for sudo when it needs it."
command -v apt-get >/dev/null || die "This script needs a Debian-based system (Raspberry Pi OS, Debian, Ubuntu)."
user="$(id -un)"

# ---------------------------------------------------------------- 1. packages
say "1/6 Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq git curl python3-venv
if ! command -v docker >/dev/null; then
    sudo apt-get install -y -qq docker.io
fi
# The compose plugin has different package names on different releases.
if ! sudo docker compose version >/dev/null 2>&1; then
    sudo apt-get install -y -qq docker-compose-plugin 2>/dev/null         || sudo apt-get install -y -qq docker-compose-v2 2>/dev/null         || die "Could not install the Docker Compose plugin. Install it, then run this script again."
fi
sudo systemctl enable --now docker >/dev/null
if ! id -nG "$user" | tr ' ' '\n' | grep -qx docker; then
    sudo usermod -aG docker "$user"
    echo "Added $user to the 'docker' group (takes effect next time you log in)."
fi

if [ "$install_orca" = 1 ]; then
    sudo apt-get install -y -qq flatpak
    if flatpak info com.orcaslicer.OrcaSlicer >/dev/null 2>&1; then
        echo "OrcaSlicer is already installed."
    else
        echo "Installing OrcaSlicer (this is a large download)..."
        flatpak remote-add --user --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
        flatpak install --user -y --noninteractive flathub com.orcaslicer.OrcaSlicer \
            || warn "OrcaSlicer did not install. The site still works, but jobs can't be sliced until it does. See README 'OrcaSlicer'."
    fi
fi

# ------------------------------------------------------------------ 2. python
say "2/6 Creating the Python environment"
want="$(cat .python-version)"
python=""
for candidate in "python$want" python3; do
    if command -v "$candidate" >/dev/null && \
       [ "$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" = "$want" ]; then
        python="$candidate"
        break
    fi
done
[ -n "$python" ] || die "JohnCad needs Python $want, and this system has $(python3 --version 2>&1).
Raspberry Pi OS 13 ('Trixie') includes it. On older releases, upgrade the OS or
install Python $want (for example with pyenv), then run this script again."
[ -x .venv/bin/python ] || "$python" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# -------------------------------------------------------------------- 3. .env
say "3/6 Writing settings (.env)"
if [ -f .env ]; then
    echo ".env already exists; leaving it alone."
else
    secret() { .venv/bin/python -c "import secrets; print(secrets.token_urlsafe($1))"; }
    hosts="localhost,127.0.0.1,$(hostname),$(hostname).local"
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [ -n "$ip" ] && hosts="$hosts,$ip"
    umask 077
    SECRET_KEY="$(secret 50)" POSTGRES_PASSWORD="$(secret 24)" ALLOWED_HOSTS="$hosts" \
    .venv/bin/python - <<'PY'
import os
import re

text = open('.env.example', encoding='utf-8').read()
values = {name: os.environ[name] for name in (
    'SECRET_KEY', 'POSTGRES_PASSWORD', 'ALLOWED_HOSTS',
)}
values['DEBUG'] = 'False'
values['BAMBUDDY_VIA_AGENT'] = 'True'
for name, value in values.items():
    text, count = re.subn(rf'(?m)^{name}=.*$', lambda m: f'{name}={value}', text, count=1)
    if not count:
        raise SystemExit(f'.env.example has no {name}= line')
open('.env', 'w', encoding='utf-8').write(text)
PY
    umask 022
    echo "Wrote .env with new secrets. It is readable only by you; don't share it."
fi

# ---------------------------------------------------------------- 4. database
say "4/6 Starting the database"
dc up -d db
wait_for_db
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py collectstatic --noinput >/dev/null
mkdir -p private_media/slicing_tmp
if command -v flatpak >/dev/null && flatpak info com.orcaslicer.OrcaSlicer >/dev/null 2>&1; then
    # Let the OrcaSlicer sandbox read uploads and write sliced files.
    flatpak override --user --filesystem="$ROOT/private_media" com.orcaslicer.OrcaSlicer
fi

# ---------------------------------------------------------------- 5. services
say "5/6 Installing services"
for unit in johncad.service johncad-slicer.service \
            johncad-backup.service johncad-backup.timer; do
    sed -e "s|@USER@|$user|g" -e "s|@DIR@|$ROOT|g" "deploy/systemd/$unit" \
        | sudo tee "/etc/systemd/system/$unit" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --quiet "${SERVICES[@]}" johncad-backup.timer
sudo systemctl restart "${SERVICES[@]}"
sudo systemctl start johncad-backup.timer
site_healthy || die "The site did not start. Look at: journalctl -u johncad -n 50"
echo "JohnCad is running, and a backup runs every night at 02:30."

# ------------------------------------------------------------- 6. admin login
say "6/6 Admin login"
has_admin="$(.venv/bin/python manage.py shell -c \
    "from django.contrib.auth import get_user_model; print(get_user_model().objects.filter(is_superuser=True).exists())" \
    2>/dev/null | tail -n 1)"
if [ "$has_admin" = "True" ]; then
    echo "An admin login already exists."
elif [ -t 0 ]; then
    echo "Choose a username and password for the site administrator:"
    .venv/bin/python manage.py createsuperuser
else
    warn "No admin login yet. Create one with: .venv/bin/python manage.py createsuperuser"
fi

cat <<EOF

$(printf '\033[1m')JohnCad is installed.$(printf '\033[0m')

On this Pi it answers at http://127.0.0.1:8000/ (admin at /admin/).

Still to do, each described in README.md:
  1. A web address: point a domain at this Pi and put Caddy in front
     ("A web address" in the README, deploy/caddy/Caddyfile.example).
  2. Slicer presets: "Slicer presets" - the preset .json paths go on each
     printer in /admin/ (or ORCA_SLICER_SETTINGS in .env).
  3. The printers' side: set up the print agent and Bambuddy on the Pi at
     the printers (agent/README.md), then add each printer in /admin/.
  4. Backups go to $ROOT/backups. Set BACKUP_DIR in .env to a USB drive
     so they survive the SD card failing: docs/operations.md.

Later:  scripts/update.sh    get the newest version
        scripts/backup.sh    back up now
EOF
