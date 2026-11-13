#!/usr/bin/env bash
# Shared helpers for the scripts in this directory. Sourced, not run.

# Project root = parent of scripts/.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Services to stop and restart: the site and the slicer. The Bambuddy
# dispatcher is only part of the direct setup (no print agent), so it is
# included only where it is enabled.
# shellcheck disable=SC2034  # used by the scripts that source this file
SERVICES=(johncad johncad-slicer)
if systemctl is-enabled --quiet johncad-dispatcher 2>/dev/null; then
    SERVICES+=(johncad-dispatcher)
fi

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# Run docker compose from the project root. Right after setup adds you to the
# "docker" group the membership isn't active until you log out and in, so
# fall back to sudo when plain docker is refused.
dc() {
    if docker info >/dev/null 2>&1; then
        (cd "$ROOT" && docker compose "$@")
    else
        (cd "$ROOT" && sudo docker compose "$@")
    fi
}

# Wait until Postgres in the compose "db" service accepts connections.
wait_for_db() {
    local _i
    for _i in $(seq 1 60); do
        if dc exec -T db sh -c 'pg_isready -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    die "Postgres did not become ready. Look at: docker compose logs db"
}

# Is the site answering on the Pi itself? (cms/views.py healthz)
site_healthy() {
    local _i
    for _i in $(seq 1 30); do
        if curl -fsS -o /dev/null http://127.0.0.1:8000/healthz/; then
            return 0
        fi
        sleep 2
    done
    return 1
}
