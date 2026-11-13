# Running JohnCad

How to keep a JohnCad install healthy: updating, backing up, restoring, and
what to do when something breaks. For first-time installation see the
[README](../README.md#quick-start).

JohnCad runs on two Raspberry Pis:

- the **site Pi** (the school's is a Pi 5, at home): the website, database,
  slicer and backups. Most of this page is about it.
- the **printer Pi** (a Pi 3, at the school): Bambuddy and the print agent.
  See [The printer Pi](#the-printer-pi).

Commands below are run on the site Pi, from the JohnCad folder, unless a
heading says otherwise.

## What is running on the site Pi

| Piece | What it does | How to look at it |
|---|---|---|
| `johncad` | The website (gunicorn on `127.0.0.1:8000`) | `systemctl status johncad` |
| `johncad-slicer` | Turns approved jobs into printable files with OrcaSlicer | `journalctl -u johncad-slicer -f` |
| `johncad-backup.timer` | Runs a backup every night at 02:30 | `systemctl list-timers johncad-backup.timer` |
| Caddy | HTTPS in front of the website | `systemctl status caddy` |
| Postgres (`db`) | The database, in Docker | `docker compose ps` |

The service files are in [`deploy/systemd/`](../deploy/systemd/);
`scripts/setup-site.sh` installs them.

**Is it working?** `curl http://127.0.0.1:8000/healthz/` answers
`{"status": "ok"}` when the site is up and can reach its database. Point an
uptime monitor at `https://<your-domain>/healthz/` to be told when it isn't.
On `/queue/`, staff can see when the print agent last checked in.

Logs also go to `logs/johncad.log` (5 files of 5 MB are kept).

## Updating

On the site Pi:

```bash
scripts/update.sh
```

This takes a backup, downloads the new version, installs its requirements,
applies database changes, restarts the services and checks the site answers.
If it stops part-way, fix the cause and run it again.

Then update the printer Pi (see [The printer Pi](#the-printer-pi)): the agent
and the site share some code, so update both.

Read the release notes first when there are any: a change that needs a manual
step will say so.

## Rolling back

If the site is broken after an update, `update.sh` prints the exact commands
with the version you were on before. They are:

```bash
git checkout <previous version>
.venv/bin/pip install -r requirements.txt
sudo systemctl restart johncad johncad-slicer
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
somewhere safe: without `SECRET_KEY` everyone is logged out.

The printer Pi's data (Bambuddy's printers and API keys, the agent's key) is
not in these backups either; see [The printer Pi](#the-printer-pi).

## Restoring

```bash
scripts/restore.sh backups/20261112-023000
```

It asks you to type `restore`, stops the site, replaces the database and
uploads with the backup's, and starts the site again. The uploads that were
there are moved to `private_media.before-restore-<time>/`, not deleted;
remove that folder once you are happy.

**Moving the site to a new Pi or SD card:**

1. On the new Pi: clone JohnCad and run `scripts/setup-site.sh`.
2. Copy your old `.env` over the new one, then
   `sudo systemctl restart johncad johncad-slicer`. (If you kept the new
   `.env` instead, people only need to log in again, and you must put your
   domain and slicer paths back in it.)
3. Copy a backup folder across and run `scripts/restore.sh <that folder>`.
4. Point the domain (and the router's port forwarding) at the new Pi, and
   install Caddy again ([README](../README.md#site-pi-setup-production), step 2). The
   print agent keeps working without changes if the domain is the same.

**Restore drill.** Every change to JohnCad is tested by a job that installs
it on a clean machine, takes a backup, deletes data, restores and checks the
data came back (`scripts/restore-drill.sh`, run by CI). That proves the
scripts; it does not prove *your* drive and *your* backups are good. Once a
term, restore your latest backup onto a spare Pi or SD card and log in.

## The printer Pi

The printer Pi runs Bambuddy (in Docker) and the print agent
(`johncad-agent`). It reaches out to the site over HTTPS; nothing connects to
it. Setup is in [agent/README.md](../agent/README.md).

| Piece | How to look at it |
|---|---|
| Print agent | `systemctl status johncad-agent`, `journalctl -u johncad-agent -f` |
| Bambuddy | `docker compose ps`, `docker compose logs -f bambuddy`, web page at `http://<printer pi>:8080` |

**Updating:**

```bash
cd ~/JohnCad && git pull
sudo systemctl restart johncad-agent
```

To change Bambuddy's version, set `BAMBUDDY_VERSION` in `.env` and run
`docker compose pull bambuddy && docker compose up -d bambuddy`. Pin a
version rather than `latest`, and note it somewhere: a new Bambuddy can change
how printers behave.

**What would be lost if the SD card died:** the printers added in Bambuddy,
its API keys, and `/etc/johncad-agent.env`. All can be recreated: add the
printers again (a few minutes), make a new Bambuddy API key, and give the agent
a new key with `.venv/bin/python manage.py create_agent_key school-pi --rotate`
on the site Pi. Jobs the site hasn't sent yet are safe on the site Pi.

To keep a copy of Bambuddy's data anyway, find its volume with
`docker volume ls` (usually `johncad_bambuddy_data`), then run
`docker run --rm -v johncad_bambuddy_data:/data -v "$PWD":/backup alpine tar -C /data -cf /backup/bambuddy-data.tar .`

## Secrets

Everything secret lives in `.env` (site Pi) and `/etc/johncad-agent.env`
(printer Pi), readable only by the user that runs JohnCad. Never commit them
or paste them into an issue.

| Value | Changing it |
|---|---|
| `SECRET_KEY` | Put a new value in `.env`, restart `johncad`. Everyone is logged out. |
| `POSTGRES_PASSWORD` | Change it in Postgres first (`docker compose exec db psql -U johncad -c "ALTER USER johncad PASSWORD '...'"`), then in `.env`, then restart both services. Postgres only reads `.env` the first time it starts. |
| Print agent key | On the site Pi: `.venv/bin/python manage.py create_agent_key school-pi --rotate`. Put the new key in `/etc/johncad-agent.env` on the printer Pi and `sudo systemctl restart johncad-agent`. |
| Bambuddy API key | Create a new key in Bambuddy, put it in `/etc/johncad-agent.env`, restart `johncad-agent`, delete the old key. |

New random value: `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`

## When something is wrong

| Symptom | Look at |
|---|---|
| Site doesn't load | `systemctl status johncad`, then `journalctl -u johncad -n 50`. If it says `SECRET_KEY is not set`, `.env` is missing or incomplete. If only the domain fails, `systemctl status caddy` and the router's port forwarding. |
| "Bad Request (400)" | The address you typed isn't in `ALLOWED_HOSTS` in `.env`. |
| Login or upload fails with "CSRF" | `CSRF_TRUSTED_ORIGINS` in `.env` must list `https://<your-domain>`. |
| Jobs stay at "Waiting to slice" | `journalctl -u johncad-slicer -n 50`; README "Slicer presets". |
| Sliced jobs never reach the printer | `/queue/` shows when the agent last checked in. If it is old, `systemctl status johncad-agent` on the printer Pi, then the troubleshooting table in [agent/README.md](../agent/README.md). |
| Database errors | `docker compose ps` (is `db` up?), `docker compose logs db`. |
| Disk full | `df -h`; old backups and `private_media.before-restore-*` folders are the usual cause. |

## Versions

| | Version | Where it is set |
|---|---|---|
| Python | 3.13 | `.python-version` |
| Django | 6.1.x | `requirements.txt` |
| Postgres | 16 | `docker-compose.yml` (and the CI service in `.github/workflows/ci.yml`) |
| Bambuddy | `latest` until pinned | `BAMBUDDY_VERSION` in the printer Pi's `.env` |

Dependabot opens update pull requests weekly, and CI fails on any dependency
with a known vulnerability. Django 6.1 receives security fixes until
December 2027; plan the move to 6.2 (the next long-term release, expected
April 2027) for mid-2027. A Postgres major upgrade needs a backup, a new data
volume and a restore, so it is done by hand, not by Dependabot.
