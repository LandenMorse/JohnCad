# Running JohnCad

How to keep a JohnCad install healthy: updating, backing up, restoring, and
what to do when something breaks. For first-time installation see the
[README](../README.md#raspberry-pi-deployment-production).

All commands are run on the Pi, from the JohnCad folder.

## What is running

| Piece | What it does | How to look at it |
|---|---|---|
| `johncad` | The website (gunicorn on `127.0.0.1:8000`) | `systemctl status johncad` |
| `johncad-slicer` | Turns approved jobs into printable files with OrcaSlicer | `journalctl -u johncad-slicer -f` |
| `johncad-dispatcher` | Sends sliced jobs to Bambuddy and checks print status | `journalctl -u johncad-dispatcher -f` |
| `johncad-backup.timer` | Runs a backup every night at 02:30 | `systemctl list-timers johncad-backup.timer` |
| Postgres (`db`) | The database, in Docker | `docker compose ps` |
| Bambuddy | Talks to the printers, in Docker | `docker compose logs -f bambuddy` |

The service files are in [`deploy/systemd/`](../deploy/systemd/);
`scripts/setup-pi.sh` installs them.

**Is it working?** `curl http://127.0.0.1:8000/healthz/` answers
`{"status": "ok"}` when the site is up and can reach its database. Point an
uptime monitor at `https://<your-domain>/healthz/` to be told when it isn't.

Logs also go to `logs/johncad.log` (5 files of 5 MB are kept).

## Updating

```bash
scripts/update.sh
```

This takes a backup, downloads the new version, installs its requirements,
applies database changes, restarts the three services and checks the site
answers. If it stops part-way, fix the cause and run it again.

Read the release notes first when there are any: a change that needs a manual
step will say so.

## Rolling back

If the site is broken after an update, `update.sh` prints the exact commands
with the version you were on before. They are:

```bash
git checkout <previous version>
.venv/bin/pip install -r requirements.txt
sudo systemctl restart johncad johncad-slicer johncad-dispatcher
```

`git checkout <previous version>` leaves the folder on that fixed version.
When a fix is released, return to normal updates with `git checkout master`
and then `scripts/update.sh`.

Going back to older code does **not** undo database changes, and usually
doesn't need to: changes are written so the previous version still works with
the newer database (see [CONTRIBUTING](../CONTRIBUTING.md#database-migrations)).
If the older version does not work with the newer database, restore the
backup that `update.sh` took just before updating (next section). Everything
entered since that backup is lost, so treat it as the last resort.

## Backups

`scripts/backup.sh` writes a folder per backup:

```
backups/20261112-023000/
    db.dump          the database
    media.tar        every uploaded and sliced file
    git-commit.txt   the JohnCad version it was taken on
```

It runs every night, and before every update. The newest 14 are kept.

**Put backups on another device.** By default they are written next to the
site, on the same SD card, which protects against a bad update but not
against the card failing. Plug in a USB drive and set, in `.env`:

```
BACKUP_DIR=/media/<user>/<drive name>/johncad-backups
BACKUP_KEEP=30
```

Check the nightly run worked: `systemctl status johncad-backup.service`, and
look for a folder with today's date. Backups contain student names, emails
and files, so keep the drive somewhere private.

`.env` is not in the backups (it holds passwords). Keep a copy of it
somewhere safe: without `SECRET_KEY` everyone is logged out, and without the
Bambuddy values printing has to be set up again.

Bambuddy keeps its own data (printers, API keys) in the Docker volume
`bambuddy_data`; these scripts do not back that up. Re-adding printers by
hand takes a few minutes.

## Restoring

```bash
scripts/restore.sh backups/20261112-023000
```

It asks you to type `restore`, stops the site, replaces the database and
uploads with the backup's, and starts the site again. The uploads that were
there are moved to `private_media.before-restore-<time>/`, not deleted;
remove that folder once you are happy.

**Moving to a new Pi or SD card:**

1. On the new Pi: clone JohnCad and run `scripts/setup-pi.sh`.
2. Copy your old `.env` over the new one, then
   `sudo systemctl restart johncad johncad-slicer johncad-dispatcher`.
   (If you kept the new `.env` instead, people only need to log in again.)
3. Copy a backup folder across and run `scripts/restore.sh <that folder>`.

**Restore drill.** Every change to JohnCad is tested by a job that installs
it on a clean machine, takes a backup, deletes data, restores and checks the
data came back (`scripts/restore-drill.sh`, run by CI). That proves the
scripts; it does not prove *your* drive and *your* backups are good. Once a
term, restore your latest backup onto a spare Pi or SD card and log in.

## Secrets

Everything secret lives in `.env`, readable only by the user that runs
JohnCad. Never commit it or paste it into an issue.

| Value | Changing it |
|---|---|
| `SECRET_KEY` | Put a new value in `.env`, restart `johncad`. Everyone is logged out. |
| `POSTGRES_PASSWORD` | Change it in Postgres first (`docker compose exec db psql -U johncad -c "ALTER USER johncad PASSWORD '...'"`), then in `.env`, then restart all three services. Postgres only reads `.env` the first time it starts. |
| `BAMBUDDY_API_KEY` | Create a new key in Bambuddy, put it in `.env`, restart `johncad-dispatcher`, delete the old key. |
| `BAMBUDDY_WEBHOOK_SECRET` | Change it in `.env` and in Bambuddy's webhook settings, restart `johncad`. |
| Print agent keys | `python manage.py create_agent_key <name> --rotate` |

New random value: `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`

## When something is wrong

| Symptom | Look at |
|---|---|
| Site doesn't load | `systemctl status johncad`, then `journalctl -u johncad -n 50`. If it says `SECRET_KEY is not set`, `.env` is missing or incomplete. |
| "Bad Request (400)" | The address you typed isn't in `ALLOWED_HOSTS` in `.env`. |
| Login or upload fails with "CSRF" | `CSRF_TRUSTED_ORIGINS` in `.env` must list `https://<your-domain>`. |
| Jobs stay at "Waiting to slice" | `journalctl -u johncad-slicer -n 50`; README "OrcaSlicer". |
| Jobs sliced but never reach the printer | `journalctl -u johncad-dispatcher -n 50`; README "Bambuddy setup". |
| Database errors | `docker compose ps` (is `db` up?), `docker compose logs db`. |
| Disk full | `df -h`; old backups and `private_media.before-restore-*` folders are the usual cause. |

## Versions

| | Version | Where it is set |
|---|---|---|
| Python | 3.13 | `.python-version` |
| Django | 6.1.x | `requirements.txt` |
| Postgres | 16 | `docker-compose.yml` (and the CI service in `.github/workflows/ci.yml`) |

Dependabot opens update pull requests weekly, and CI fails on any dependency
with a known vulnerability. Django 6.1 receives security fixes until
December 2027; plan the move to 6.2 (the next long-term release, expected
April 2027) for mid-2027. A Postgres major upgrade needs a backup, a new data
volume and a restore, so it is done by hand, not by Dependabot.
