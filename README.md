# JohnCad

A Django app for a small 3D-printing pipeline: Students upload `.stl` files,
request prints, a teacher reviews and approves them, OrcaSlicer converts the
approved files to `.gcode.3mf`, and a print agent at the printers hands them
to Bambu printers through a self-hosted
[Bambuddy](https://github.com/maziggy/bambuddy). Django never talks to the
printers directly.

It runs on **two Raspberry Pis**:

```
students/teachers ──HTTPS──> site Pi (Pi 5, at home)
                             Caddy -> gunicorn (JohnCad) + Postgres + slice_worker (OrcaSlicer)
                                   ^
                                   │ HTTPS 443, outbound only, every 15 s
                                   │
printer Pi (Pi 3, at school): johncad-agent -> Bambuddy -> printers (LAN)
```

| | Site Pi | Printer Pi |
|---|---|---|
| Runs | the website, database, slicer, nightly backups | the print agent and Bambuddy |
| Where | anywhere with a public web address | on the printers' network |
| Set up with | `scripts/setup-site.sh` | [agent/README.md](agent/README.md) |

The printer Pi only makes outbound HTTPS connections, so the school network
needs no inbound access, tunnel or VPN.

## Quick start

**Running it for a class.** You need two Raspberry Pis (Raspberry Pi OS
64-bit) and a domain name.

1. **Site Pi.** Clone and run the installer:
   ```bash
   sudo apt install -y git
   git clone https://github.com/LandenMorse/JohnCad.git && cd JohnCad
   scripts/setup-site.sh
   ```
   It installs everything, starts the site and sets up nightly backups.
2. **A web address and slicer presets** on the site Pi: follow
   [Site Pi setup](#site-pi-setup-production), steps 2 and 3.
3. **Printer Pi:** follow [agent/README.md](agent/README.md), then add your
   printers in `/admin/`.

**Working on the code:** see [CONTRIBUTING.md](CONTRIBUTING.md). You can run
and test it on any computer in about five minutes, without a Pi or a printer.

## Documentation

| | |
|---|---|
| This README | Installing, configuring, and what each part of the program does |
| [docs/operations.md](docs/operations.md) | Updating, backups, restoring, rolling back, troubleshooting |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup, tests, CI, conventions, migration rules |
| [agent/README.md](agent/README.md) | The printer Pi: print agent and Bambuddy |
| [docs/bambuddy.md](docs/bambuddy.md) | How JohnCad uses Bambuddy, its API details, and the optional direct mode |
| [CLAUDE.md](CLAUDE.md) | Current implementation status and open items |

## Contents

- [Quick start](#quick-start)
- [Setup](#setup) (development)
- [Site Pi setup](#site-pi-setup-production)
- [Printer Pi](#printer-pi-print-agent-and-bambuddy)
- [Helper commands](#helper-commands)
- [Program layout](#program-layout)
- [Models](#models)
- [Features](#features)

## Setup

For local development. The shortest path (SQLite, no Docker) is in
[CONTRIBUTING.md](CONTRIBUTING.md); the steps below use Postgres, like
production.

### Requirements

- Python 3.13
- Docker + Docker Compose (for Postgres locally, and for the full
  containerized stack)
- (Production only) OrcaSlicer, installed as a Flatpak
  (`com.orcaslicer.OrcaSlicer`), on the host running Django

### 1. Clone and create a virtualenv

```bash
git clone <repo-url> JohnCad
cd JohnCad
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash
# or: source .venv/bin/activate  # Linux/Mac
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Then edit `.env` as needed. All variables (see `.env.example` for the full,
commented list):

| Variable | Purpose                                                                                                                   |
|---|---------------------------------------------------------------------------------------------------------------------------|
| `SECRET_KEY` | Django secret key — change for anything beyond local dev                                                                  |
| `DEBUG` | `True`/`False`                                                                                                            |
| `ALLOWED_HOSTS` | Comma-separated hostnames                                                                                                 |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated origins, e.g. `https://print.example.org` — needed once served through Caddy on a real domain             |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | Postgres credentials                                                                                                      |
| `POSTGRES_HOST` | `localhost` for host-venv dev; `docker compose up` overrides this to `db` automatically for the containerized web service |
| `POSTGRES_PORT` | Default `5432`                                                                                                            |
| `ORCA_SLICER_CMD` | Command to launch OrcaSlicer, default `flatpak run com.orcaslicer.OrcaSlicer`                                             |
| `ORCA_SLICER_SETTINGS` | Semicolon-separated machine + process preset `.json` files (`--load-settings`); required — blank = slicer not configured  |
| `ORCA_SLICER_FILAMENTS` | Semicolon-separated filament preset `jm.json` file(s) (`--load-filaments`), optional                                      |
| `ORCA_SLICER_DATADIR` | Optional OrcaSlicer `--datadir`                                                                                           |
| `ORCA_SLICER_TIMEOUT` | Slicing timeout in seconds, default `300`                                                                                 |
| `HTTPS_ENABLED` | `True` once the site is served over HTTPS (marks cookies Secure) |
| `BAMBUDDY_VIA_AGENT` | `True` (the supported setup): a print agent on the printer Pi pulls jobs from `/agent-api/` (see [`agent/`](agent/README.md)). `setup-site.sh` sets it |
| `BAMBUDDY_STAGED` | `True` (default): jobs wait in Bambuddy until staff release them after clearing the plate |
| `AGENT_CLAIM_TIMEOUT` | Seconds before a job an agent stopped downloading is offered again, default `900` |
| `AGENT_POLL_INTERVAL` | Seconds between agent check-ins, sent to agents, default `15` |
| `BACKUP_DIR` / `BACKUP_KEEP` | Where `scripts/backup.sh` writes (default `backups/`; use a USB drive) and how many to keep (default 14) |
| `BAMBUDDY_URL` / `BAMBUDDY_API_KEY` / `BAMBUDDY_WEBHOOK_SECRET` / `BAMBUDDY_PRINTER_ID` / `BAMBUDDY_TIMEOUT` | **Direct mode only** (no print agent; [docs/bambuddy.md](docs/bambuddy.md)). With an agent these live in the printer Pi's `/etc/johncad-agent.env` instead |
| `BAMBUDDY_VERSION` / `BAMBUDDY_PORT` | Image tag and port for the compose `bambuddy` service (set on the printer Pi) |

### 3. Start Postgres

```bash
docker compose up -d db
```

### 4. Migrate and create a superuser

```bash
python manage.py migrate
python manage.py createsuperuser
```

### 5. Run the dev server

```bash
python manage.py runserver
```

Visit `http://127.0.0.1:8000/`. Log into `/admin/` with the superuser you just
created. Teachers: create a user with **Staff status** ticked (not
superuser). A teacher creates a class at `/classes/` and generates a signup
code; Students sign up at `/signup/` with that code. Teachers approve jobs at
`/review/`.

### Site Pi setup (production)

The site Pi (the school's is a Pi 5) runs **gunicorn directly on the host**, so
it can call the OrcaSlicer Flatpak, **Postgres in Docker**, and **Caddy** for
HTTPS. Day-to-day running (updates, backups, restoring, troubleshooting) is in
[docs/operations.md](docs/operations.md).

**1. Install.** On the Pi (Raspberry Pi OS 64-bit; Python 3.13 is required,
which Raspberry Pi OS 13 "Trixie" includes):

```bash
sudo apt install -y git
git clone https://github.com/LandenMorse/JohnCad.git && cd JohnCad
scripts/setup-site.sh
```

The script is safe to run again, and does the following:

| Step | What happens |
|---|---|
| Packages | Installs Docker, Python venv support, Flatpak and the OrcaSlicer Flatpak (`--no-orca` skips OrcaSlicer) |
| Python | Creates `.venv/` and installs `requirements.txt` |
| Settings | Writes `.env` from `.env.example` with a new `SECRET_KEY` and `POSTGRES_PASSWORD`, `DEBUG=False`, `BAMBUDDY_VIA_AGENT=True`, and this Pi's names in `ALLOWED_HOSTS`. An existing `.env` is never touched |
| Database | Starts Postgres (`docker compose up -d db`), runs `migrate` and `collectstatic` |
| Services | Installs the units in [`deploy/systemd/`](deploy/systemd/): `johncad` (gunicorn on `127.0.0.1:8000`), `johncad-slicer`, and a nightly `johncad-backup` timer |
| Admin login | Asks for a username and password if no admin exists yet |

When it finishes, the site answers on the Pi itself at
`http://127.0.0.1:8000/`. Gunicorn listens on localhost only; the next step
makes it reachable. Static files are served by WhiteNoise.

Without `johncad-slicer` running, approved jobs sit at "Waiting to slice". For
a one-off run without the service: `python manage.py slice_worker --once`.

**2. A web address: Caddy.** Needs a domain name pointing at the Pi (an
A/AAAA record; use a dynamic-DNS client if your home IP changes) with TCP
ports 80 and 443 forwarded to it from your router.

```bash
sudo apt install -y caddy
sudo cp deploy/caddy/Caddyfile.example /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile        # replace print.example.org with your domain
sudo systemctl reload caddy
```

Caddy fetches and renews the HTTPS certificate by itself and redirects
`http://` to `https://`. It returns 404 for `/api/` (the local-only Bambuddy
webhook of the optional direct mode); the print agent's `/agent-api/` stays
reachable. Then set, in `.env`:

```
ALLOWED_HOSTS=<your-domain>,localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=https://<your-domain>
HTTPS_ENABLED=True
```

and `sudo systemctl restart johncad johncad-slicer`. Don't put comments on the
same line as a value in `.env`: systemd doesn't strip them. Uploads up to
512 MB are allowed (`max_size` in the Caddyfile, `DATA_UPLOAD_MAX_MEMORY_SIZE`
in `cms/settings.py`).

**3. Slicer presets.** Put the Bambu machine/process/filament preset `.json`
files somewhere on the Pi (e.g. `/home/<user>/orca/`) and let the OrcaSlicer
sandbox read them (the setup script already did this for `private_media/`):

```bash
flatpak override --user --filesystem=/home/<user>/orca:ro com.orcaslicer.OrcaSlicer
```

Enter the paths on each Printer in `/admin/` (see the printer Pi section), or
for a single-printer setup in `.env`:

```
ORCA_SLICER_SETTINGS=/home/<user>/orca/machine.json;/home/<user>/orca/process.json
ORCA_SLICER_FILAMENTS=/home/<user>/orca/filament.json
```

After editing `.env`, run `sudo systemctl restart johncad johncad-slicer`.

Smoke-test slicing before relying on it:

```bash
mkdir -p private_media/slicing_tmp
flatpak run com.orcaslicer.OrcaSlicer --slice 0 --outputdir $PWD/private_media/slicing_tmp   --load-settings "/home/<user>/orca/machine.json;/home/<user>/orca/process.json" some_model.stl
```

It should write a `.gcode` file. Add `--export-3mf out.gcode.3mf` to check
the `.gcode.3mf` that JohnCad actually sends to Bambuddy. If it fails for lack of a display, try
`ORCA_SLICER_CMD=xvfb-run -a flatpak run com.orcaslicer.OrcaSlicer` in `.env`
(`sudo apt install xvfb`).

**4. The printers.** Set up the [printer Pi](#printer-pi-print-agent-and-bambuddy).

**5. Keep it healthy.** To get a new version later:

```bash
scripts/update.sh
```

It backs up first, then updates, restarts and checks the site. Backups run
nightly into `backups/`; set `BACKUP_DIR` in `.env` to a USB drive so they
survive the SD card failing. Restoring, rolling back and troubleshooting:
[docs/operations.md](docs/operations.md).

<details>
<summary>Installing by hand instead of with the script</summary>

```bash
sudo apt update
sudo apt install -y git curl flatpak python3-venv docker.io docker-compose-plugin
sudo usermod -aG docker $USER      # log out/in afterwards
flatpak remote-add --user --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
flatpak install --user flathub com.orcaslicer.OrcaSlicer

python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env     # then set SECRET_KEY, DEBUG=False, BAMBUDDY_VIA_AGENT=True,
                         # ALLOWED_HOSTS, POSTGRES_PASSWORD (before the first
                         # "docker compose up": Postgres only reads it when it
                         # creates its volume)

docker compose up -d db
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py createsuperuser
flatpak override --user --filesystem=$PWD/private_media com.orcaslicer.OrcaSlicer

# Services: replace @USER@ and @DIR@ in johncad.service, johncad-slicer.service,
# johncad-backup.service and johncad-backup.timer under deploy/systemd/, copy
# them to /etc/systemd/system/, then:
sudo systemctl daemon-reload
sudo systemctl enable --now johncad johncad-slicer johncad-backup.timer
```

</details>

### Printer Pi: print agent and Bambuddy

A second Pi on the printers' network (the school's is a Pi 3) runs Bambuddy in
Docker and the print agent as a service. The full steps are in
[agent/README.md](agent/README.md). In short:

1. **Site Pi:** create the agent's key in `/admin/` > **Printer agents**
   (or `python manage.py create_agent_key school-pi`).
2. **Printer Pi:** clone JohnCad, run `docker compose up -d bambuddy`, add
   the printers in Bambuddy (LAN Mode on) and print one `.gcode.3mf` by hand,
   create a Bambuddy API key, put both keys and the site's address in
   `/etc/johncad-agent.env`, and install the `johncad-agent` service.
3. **Site Pi, `/admin/` > Printers:** add each printer: a name teachers will
   see, its Bambuddy printer id, and the OrcaSlicer machine + process preset
   `.json` paths for that printer's model. Teachers then choose the printer
   when approving a job on `/review/`.

The printer Pi needs outbound HTTPS to the site's domain and nothing else; ask
school IT to allow that domain if it is blocked. More on the network and the
Bambuddy API: [docs/bambuddy.md](docs/bambuddy.md).

### OrcaSlicer

Slicing (`jobs/slicing.py`) shells out to the OrcaSlicer CLI, installed as a
Flatpak on the Pi and run on the host alongside Django (not in Docker). Point
`ORCA_SLICER_SETTINGS` / `ORCA_SLICER_FILAMENTS` at the installed Bambu
machine/process/filament preset `.json` files.

The Flatpak sandbox must be able to read the STL and preset files and write
its output. Scratch output goes under `MEDIA_ROOT/slicing_tmp`, so grant:

```bash
flatpak override --user --filesystem=/path/to/JohnCad/private_media com.orcaslicer.OrcaSlicer
flatpak override --user --filesystem=/path/to/preset/dir:ro com.orcaslicer.OrcaSlicer
```

Smoke-test headless on the Pi before relying on it:
`flatpak run com.orcaslicer.OrcaSlicer --slice 0 --outputdir /out --load-settings "machine.json;process.json" in.stl`.

## Helper commands

Standard Django management commands, run from the project root with the
virtualenv active:

```bash
python manage.py runserver              # dev server
python manage.py migrate                # apply migrations
python manage.py makemigrations         # generate migrations after model changes
python manage.py createsuperuser        # create an admin login
python manage.py collectstatic          # gather static files (needed in prod; setup-site.sh and update.sh do it)
python manage.py check                  # system checks, catches config errors
python manage.py shell                  # interactive Python shell with models loaded
```

Scripts (site Pi; see [docs/operations.md](docs/operations.md)):

```bash
scripts/setup-site.sh            # first-time install (safe to run again)
scripts/update.sh                # back up, update, migrate, restart, health check
scripts/backup.sh                # back up now
scripts/restore.sh <backup dir>  # restore a backup
```

Docker Compose (the site's Postgres; Bambuddy on the printer Pi):

```bash
docker compose up -d db          # Postgres (the setup script does this)
docker compose logs -f db        # its logs
docker compose up -d bambuddy    # printer Pi only: Bambuddy
```

Useful one-offs via `manage.py shell -c "..."`:

```bash
# Create a print agent and print its key (shown once); --rotate replaces it
python manage.py create_agent_key school-pi

# Slice everything waiting, then exit (the johncad-slicer service does this continuously)
python manage.py slice_worker --once

# Direct mode only: send waiting jobs to Bambuddy and poll once
python manage.py dispatch_worker --once
```

## Program layout

Three Django apps, split by feature, plus one project-level shared template:

```
JohnCad/
├── cms/                     # Django project (settings, root urls, wsgi/asgi)
│   ├── settings.py          # all configuration, env-driven (see .env.example)
│   ├── views.py             # /healthz/
│   └── test_runner.py       # lets tests run from a clean clone
├── templates/base.html      # shared layout every app's templates extend
├── accounts/                 # Student identity
│   ├── views.py                 # CmsLoginView
│   ├── urls.py                   # login/, logout/
│   ├── admin.py                    # re-registers User in the admin
│   └── templates/accounts/          # login.html
├── attachments/               # raw .stl upload & ownership
│   ├── models.py                # Attachment
│   ├── admin.py                   # AttachmentAdmin
│   ├── forms.py                     # AttachmentForm
│   ├── views.py                       # home/upload, delete, download
│   ├── urls.py                          # '', attachments/<id>/{delete,download}/
│   ├── migrations/                        # schema history
│   └── templates/attachments/              # attachment_list.html
├── jobs/                       # the print pipeline
│   ├── models.py                 # Job (FK -> attachments.Attachment)
│   ├── admin.py                    # JobAdmin + approve/reject actions
│   ├── slicing.py                    # OrcaSlicer CLI integration
│   ├── bambuddy_client.py              # Bambuddy REST API details + client, no Django (shared with agent/)
│   ├── bambuddy.py                     # direct mode: Django wrapper for it (settings) + webhook parsing
│   ├── dispatch.py                     # print agent state changes; direct mode: dispatch, status sync, release-next
│   ├── agent_api.py, agent_urls.py     # print agent API, mounted at /agent-api/v1/
│   ├── management/commands/            # slice_worker, create_agent_key, dispatch_worker (direct mode)
│   ├── views.py                        # attachment_export, queue, review
│   ├── urls.py                           # attachments/<id>/export/, queue/, review/
│   ├── api_views.py, api_urls.py           # direct mode: Bambuddy webhook, mounted at /api/
│   ├── migrations/                           # schema history
│   └── templates/jobs/                         # queue.html
├── agent/                      # print agent for the printer Pi (johncad_agent.py, systemd unit, README)
├── scripts/                    # setup-site.sh, update.sh, backup.sh, restore.sh, restore-drill.sh
├── deploy/systemd/             # service and backup-timer units that setup-site.sh installs
├── deploy/caddy/               # Caddyfile example (HTTPS in front of the site)
├── .github/                    # CI workflow, Dependabot, pull request template
├── static/css/site.css        # the site's one stylesheet
├── docker-compose.yml           # db (site Pi) and bambuddy (printer Pi) services
├── requirements.txt                  # Python dependencies
├── requirements-dev.txt              # ruff and pip-audit, for development and CI
├── .env.example                       # documented list of all env vars
├── CLAUDE.md                           # architecture notes, current status, open items
├── CONTRIBUTING.md                     # development setup, tests, CI, conventions
├── docs/operations.md                  # updating, backups, restoring, troubleshooting
└── docs/bambuddy.md                    # how JohnCad uses Bambuddy, its API facts, direct mode
```

Dependency direction is one-way: `jobs` imports from `attachments` (the `Job.attachment`
FK, and `attachment_export`'s need for the `Attachment` model) — never the
reverse, aside from `attachments`'s home view doing one read-only `Job`
query to populate the "My Jobs" table on a Student's dashboard.

## Models

`Attachment` lives in `attachments/models.py`; `Job` lives in `jobs/models.py`.

### `Attachment`

The raw file a Student uploads.

| Field | Notes |
|---|---|
| `user` | FK to `User`, `related_name='attachments'` |
| `name` | Display name given at upload time |
| `file` | The `.stl` itself — `FileExtensionValidator` restricts to `.stl`. Stored under `MEDIA_ROOT` (`private_media/`), never mounted at a public URL — only reachable via the owner-only `attachment_download` view |
| `uploaded_at` | Auto-set on creation |

One Attachment can have many `Job`s (a Student can export/request-print the
same file more than once).

### `Job`

One specific request to print an `Attachment`. This is the thing that
actually moves through the approval → slicing → printing pipeline.

| Field | Notes |
|---|---|
| `user` | FK to `User`, `related_name='jobs'` — who requested this print |
| `attachment` | FK to `Attachment`, `related_name='jobs'` — what's being printed |
| `status` | One of `pending`/`approved`/`queued`/`working`/`finished`/`rejected`/`failed` — see status table below |
| `sliced_file` | Sliced `.gcode.3mf`, populated by `slicing.slice_to_3mf()` once a job is approved and slicing succeeds. Blank until then. |
| `created_at` | Set when the Student clicks Export |
| `slicing_started_at` | Set while the slice worker is slicing the job; blank otherwise |
| `slicing_error` | Why the last slice failed; blank if none. Cleared on retry |
| `approved_at` | Set when an admin approves; drives FIFO ordering of the queue |
| `bambuddy_file_id` / `bambuddy_queue_id` | Bambuddy's ids for the uploaded file and the queue item; set by the print agent (or the dispatch worker in direct mode) |
| `dispatch_started_at` | Set while the dispatch worker is sending the job |
| `dispatch_error` | Why the last dispatch or print failed; blank if none. Cleared on retry |
| `dispatched_at` | When the job reached Bambuddy's queue |
| `released_at` | When staff released it after clearing the plate |
| `working_at` / `finished_at` / `failed_at` | Set from the print agent's reports (or Bambuddy's webhook, in direct mode) |

#### Job status lifecycle

| Status | Set by | Meaning |
|---|---|---|
| `pending` | Student (Export click) | Awaiting admin review |
| `approved` | Admin / teacher | Signed off; waiting for the `slice_worker` to slice it (shown as Waiting to slice / Slicing… / Slicing failed) |
| `queued` | System, automatically | Sliced; being sent to Bambuddy by the print agent, then waiting there for the plate to be cleared (FIFO by `approved_at`) |
| `working` | Print agent (reads Bambuddy's queue) | The print started |
| `finished` | Print agent | The print completed |
| `failed` | Print agent | The print failed or was stopped; a teacher can Retry it |
| `rejected` | Admin | Declined instead of approved — terminal |

If slicing fails or the slicer isn't configured, the job stays parked in
`approved` rather than silently disappearing — the reason
(`SlicerNotConfigured`, e.g. binary missing, or `SlicingFailed` with
OrcaSlicer's captured stderr) is saved in `Job.slicing_error` and shown on the
review page, where a teacher can click **Retry**.

## Features

### Accounts

Standard Django session auth: `/login/` and `/logout/` (POST-only, per
Django 5+). No password reset flow currently.

### Classes & signup codes (`/classes/`, `/signup/`)

Teachers create classes and generate **signup codes** for them. A code is 8
characters (no look-alike 0/O/1/I/L), works for any number of students, and
expires 24 hours after it's generated (or when revoked). `/signup/` asks for
the code, username, first/last name, email and password, creates a regular
(non-staff) account and puts it in the code's class; the account keeps
working after the code expires. The class page shows a `/signup/?code=…` link
that pre-fills the code. Students who already have an account join further
classes by entering a code on `/classes/`.

A class can have several teachers; any of them can manage the roster
(remove students), codes and co-teachers, but the last teacher can't be
removed. Superusers see and manage every class. Classes and codes are also
editable in `/admin/`.

When a Student exports an attachment they pick which of their classes it's
for (no picker if they're in exactly one; students in no class submit
without one). The review page only shows a teacher jobs submitted to their
classes, plus their own; superusers see everything, including classless
jobs, and the page has a class filter.

### Attachment upload & management (`/`)

A Student's home page: upload form (name + `.stl` file), a table of their
own attachments with download/export/delete actions, and a "My Jobs" table
showing every job they've ever requested and its current status. All scoped
to `request.user` — Students never see each other's attachments here.

### Export → Job creation

Clicking **Export** on an attachment (`attachment_export` view, in
`jobs/views.py` — it creates a `Job`, so it lives with `jobs` even though
its URL is still `attachments/<id>/export/`) creates a new `Job` in
`pending` status. A Student can export the same attachment multiple times,
producing multiple independent jobs.

### Teacher review page (`/review/`)

Staff accounts (`is_staff`, e.g. teachers) approve or reject jobs at
`/review/` (`jobs/views.py`, shared logic in `jobs/services.py`). It lists
every job — new and past — newest first, with **Approve** / **Reject**
buttons for `pending` jobs and **Retry** for jobs whose slicing failed. It
shows Waiting to slice / Slicing… / Slicing failed (with the reason) for
approved jobs and refreshes itself while any are in progress. Non-staff users
get a 403.

### Admin (`/admin/`, superusers only)

The Django admin is restricted to superusers (`cms/admin.py`; staff-only
accounts can't log in). To make a teacher account, tick **Staff status** only.
It contains `Job` (registered in `jobs/admin.py`, changelist at
`/admin/jobs/job/`) alongside the default `User` (`accounts/admin.py`) and
`Attachment` (`attachments/admin.py`) admin. Two custom bulk actions on the
Job changelist:

- **Approve selected jobs** — moves `pending → approved` and stamps
  `approved_at`; the slice worker then slices it (see status table above).
- **Reject selected jobs** — moves `pending → rejected`.

### Slicing (`jobs/slicing.py`)

The `slice_worker` management command (`jobs/management/commands/`, logic in
`jobs/services.py`) polls for `approved` jobs without a sliced file, claims the oldest
one atomically, and calls `slice_to_3mf`. Failures are stored on the job, so
a failing job isn't retried in a loop until a teacher clicks Retry. Claims left
by a crashed worker are released after `ORCA_SLICER_TIMEOUT` + 60s.

`slice_to_3mf(job)` invokes OrcaSlicer as a subprocess
(`<ORCA_SLICER_CMD> --slice 0 --outputdir <tmp> --load-settings <settings>
[--load-filaments <filaments>] [--datadir <dir>] <stl>`), picks up the
generated `.gcode.3mf` (`--export-3mf`) into `job.sliced_file`, and advances
the job to `queued`. No shell interpolation (`shell=True`) is used. Configurable via
the `ORCA_SLICER_*` env vars above.

### Public print queue (`/queue/`)

Any logged-in Student (not just admins) can see what's currently `queued` or
`working` — attachment name, uploader's username, and status. Read-only;
there's no way for a Student to reorder or cancel jobs from here.

### Print agent (`/agent-api/v1/`, `BAMBUDDY_VIA_AGENT=True`)

How jobs reach the printers: a print agent next to Bambuddy on the printer Pi
(`agent/johncad_agent.py`, run as a systemd service) polls the site over HTTPS,
claims sliced jobs, downloads them with HTTP Range requests (resumable,
SHA-256 checked), uploads and enqueues them in its Bambuddy as staged items,
and reports the queue item back. Staff see **Plate cleared, release next** on
`/queue/`, which only records a request; the agent carries it out on its next
check-in (a failure, such as a short spool, shows on `/queue/`). Failures are
stored on the job and shown on `/review/` with a **Retry** button, and a failed
print can be retried the same way. The agent reports print status from
Bambuddy's queue. Agents authenticate with a key managed in `/admin/` >
Printer agents (stored hashed, revocable, optionally limited to some
printers). API and setup: [`agent/README.md`](agent/README.md).

### Direct mode (optional): dispatch worker and webhook

For a setup where Bambuddy runs on the same Pi as the site
(`BAMBUDDY_VIA_AGENT=False`; not used at the school). The `dispatch_worker`
command (logic in `jobs/dispatch.py`, Bambuddy calls in `jobs/bambuddy.py`)
claims the oldest sliced `queued` job, uploads its `.gcode.3mf` as
`johncad-job-<id>.gcode.3mf`, and adds it to the printer's Bambuddy queue as a
staged item, polling each queue item every 60 s. Bambuddy reports print events
to `POST /api/bambuddy/webhook/` (`BAMBUDDY_WEBHOOK_SECRET` as
`Authorization: Bearer <secret>` or `?secret=`; Caddy returns 404 for `/api/`
so only Bambuddy on the same Pi reaches it). Jobs are matched by the
`johncad-job-<id>` name Bambuddy reports as `filename`: `print_start` moves a
job to `working`, `print_complete` to `finished`, `print_failed` and
`print_stopped` to `failed`; repeats are no-ops and invalid transitions return
`409`. Setup notes: [docs/bambuddy.md](docs/bambuddy.md).

### Deployment

Production is two Raspberry Pis (see the top of this file). The site Pi runs
gunicorn and the slicer on the host, installed by `scripts/setup-site.sh`;
Postgres runs in Docker (`docker-compose.yml`, `db` service); Caddy provides
HTTPS. Static files are served by WhiteNoise from within the Django process,
so no separate static file server is needed. The printer Pi runs Bambuddy
(`docker-compose.yml`, `bambuddy` service) and the print agent. Every change is
checked by CI, including a full install of the site, a print agent connecting
to it, and a backup-and-restore drill (see
[CONTRIBUTING.md](CONTRIBUTING.md#what-ci-checks)).

The site is not run in Docker: slicing needs the host's OrcaSlicer Flatpak.
