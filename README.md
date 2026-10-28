# JohnCad

A Django app for a small 3D-printing pipeline: Students upload `.stl` files,
request prints, an admin reviews and approves them, OrcaSlicer converts the
approved files to G-code, and an Arduino UNO R4 WiFi controller at the
printers pulls finished jobs down to feed to the printers via microSD card.

Two different Pi/board references show up in this project — don't confuse
them: the **JohnCad server** (Django + Postgres) is intended to run on a
Raspberry Pi; the **printer controller** at the printers is a separate
Arduino UNO R4 WiFi, not a Pi at all.

See `CLAUDE.md` for the current implementation status and open items, and
`PRINTER_CONTROLLER_API.md` for the printer controller's API contract in detail.

## Contents

- [Setup](#setup)
- [Raspberry Pi deployment](#raspberry-pi-deployment-production)
- [Helper commands](#helper-commands)
- [Program layout](#program-layout)
- [Models](#models)
- [Features](#features)

## Setup

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
| `CSRF_TRUSTED_ORIGINS` | Comma-separated origins, e.g. `https://rhsprint.crabdance.com` — needed once served through nginx on a real domain        |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | Postgres credentials                                                                                                      |
| `POSTGRES_HOST` | `localhost` for host-venv dev; `docker compose up` overrides this to `db` automatically for the containerized web service |
| `POSTGRES_PORT` | Default `5432`                                                                                                            |
| `ORCA_SLICER_CMD` | Command to launch OrcaSlicer, default `flatpak run com.orcaslicer.OrcaSlicer`                                             |
| `ORCA_SLICER_SETTINGS` | Semicolon-separated machine + process preset `.json` files (`--load-settings`); required — blank = slicer not configured  |
| `ORCA_SLICER_FILAMENTS` | Semicolon-separated filament preset `jm.json` file(s) (`--load-filaments`), optional                                      |
| `ORCA_SLICER_DATADIR` | Optional OrcaSlicer `--datadir`                                                                                           |
| `ORCA_SLICER_TIMEOUT` | Slicing timeout in seconds, default `300`                                                                                 |

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

### Running the full stack in Docker (dev/experimental — no slicing)

**Note:** the `web` container can't reach the host's OrcaSlicer Flatpak, so
approving jobs from inside it won't slice (jobs park in `approved`). For
real use on the Pi, follow [Raspberry Pi deployment](#raspberry-pi-deployment-production).

Instead of steps 3–5, build and run everything — Postgres, gunicorn, and
nginx in front:

```bash
docker compose up -d --build
```

This serves the site on `http://localhost/` (port 80, via nginx). See
`docker-compose.yml` for the three services (`db`, `web`, `nginx`) and
`nginx/nginx.conf` for the reverse-proxy config, which includes a
commented-out HTTPS block ready for Let's Encrypt certs. This is the
deployment shape intended for the Raspberry Pi — build natively on the Pi
(`docker compose build`) rather than cross-compiling, since `python:3.13-slim`
publishes official arm64 images.

### Raspberry Pi deployment (production)

Production shape: **Django (gunicorn) runs directly on the Pi** so it can call
the OrcaSlicer Flatpak; **Postgres runs in Docker**. Everything below is run
on the Pi after cloning.

**1. Install prerequisites** (Raspberry Pi OS 64-bit recommended):

```bash
sudo apt update
sudo apt install -y git flatpak python3-venv docker.io docker-compose-plugin
sudo usermod -aG docker $USER      # log out/in afterwards
flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
flatpak install flathub com.orcaslicer.OrcaSlicer   # skip if already installed
```

Python 3.13 is required. If `python3 --version` is older, install 3.13
(e.g. via `pyenv` or a deadsnakes-style build) and use it for the venv.

**2. Clone and install:**

```bash
git clone <repo-url> JohnCad && cd JohnCad
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**3. Configure `.env`** (`cp .env.example .env`, then edit):

```
SECRET_KEY=<long random string: python -c "import secrets; print(secrets.token_urlsafe(50))">
DEBUG=False
ALLOWED_HOSTS=rhsprint.crabdance.com,<pi-ip>,localhost
CSRF_TRUSTED_ORIGINS=https://rhsprint.crabdance.com
HTTPS_ENABLED=False
POSTGRES_PASSWORD=<strong password>
POSTGRES_HOST=localhost
ORCA_SLICER_SETTINGS=/home/<user>/orca/machine.json;/home/<user>/orca/process.json
ORCA_SLICER_FILAMENTS=/home/<user>/orca/filament.json
```

Use the absolute paths of the Bambu machine/process/filament preset `.json`
files already on the Pi. Set `POSTGRES_PASSWORD` **before** the first
`docker compose up` — Postgres only reads it when it initializes its volume.

**4. Start Postgres, migrate, create the admin login:**

```bash
docker compose up -d db
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

**5. Let the Flatpak sandbox see your files** (same Linux user that runs
Django):

```bash
flatpak override --user --filesystem=$PWD/private_media com.orcaslicer.OrcaSlicer
flatpak override --user --filesystem=/home/<user>/orca:ro com.orcaslicer.OrcaSlicer
```

**6. Smoke-test slicing before relying on it:**

```bash
mkdir -p private_media/slicing_tmp
flatpak run com.orcaslicer.OrcaSlicer --slice 0 --outputdir $PWD/private_media/slicing_tmp   --load-settings "/home/<user>/orca/machine.json;/home/<user>/orca/process.json" some_model.stl
```

It should write a `.gcode` file. If it fails for lack of a display, try
`ORCA_SLICER_CMD=xvfb-run -a flatpak run com.orcaslicer.OrcaSlicer` in `.env`
(`sudo apt install xvfb`). This path is untested on real hardware.

**7. Run Django as a service.** Create `/etc/systemd/system/johncad.service`
(replace `<user>` and the path):

```ini
[Unit]
Description=JohnCad (gunicorn)
After=network.target docker.service

[Service]
User=<user>
WorkingDirectory=/home/<user>/JohnCad
EnvironmentFile=/home/<user>/JohnCad/.env
ExecStart=/home/<user>/JohnCad/.venv/bin/gunicorn cms.wsgi:application --bind 127.0.0.1:8000 --workers 3 --timeout 900
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now johncad
sudo systemctl status johncad
```

**Slicing worker.** Approving a job doesn't slice it in the web request; the
`slice_worker` command does, in the background. It must run on the host (next
to the OrcaSlicer Flatpak) with the same `.env`. Create
`/etc/systemd/system/johncad-slicer.service`:

```ini
[Unit]
Description=JohnCad slicing worker
After=network.target docker.service

[Service]
User=<user>
WorkingDirectory=/home/<user>/JohnCad
EnvironmentFile=/home/<user>/JohnCad/.env
ExecStart=/home/<user>/JohnCad/.venv/bin/python manage.py slice_worker
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now johncad-slicer
journalctl -u johncad-slicer -f     # watch it slice
```

Without this service running, approved jobs just sit at "Waiting to slice".
Jobs approved before the worker existed are picked up automatically. For a
one-off run without the service: `python manage.py slice_worker --once`.

Gunicorn listens on localhost only; nginx (next step) is the public entry
point. Static files are served by WhiteNoise. The compose `db` service has `restart: unless-stopped`, so
Postgres comes back after reboots. The compose `web`/`nginx` services and `nginx/nginx.conf` are not
used in this setup (the `web` container can't reach the host's Flatpak);
nginx runs on the host using `nginx/johncad.host.conf`.

**8. nginx + HTTPS.** Requires a real domain name pointing at the Pi (A/AAAA
record) with ports 80 and 443 forwarded to it from your router. The printer
controller uses `WiFiSSLClient` against `https://rhsprint.crabdance.com/api`, so a
publicly trusted certificate (Let's Encrypt) is the intended setup — a
self-signed cert would not be trusted by the Arduino.

```bash
sudo apt install -y nginx certbot
sudo mkdir -p /var/www/certbot
sudo cp nginx/johncad.host.conf /etc/nginx/sites-available/johncad
sudo ln -s /etc/nginx/sites-available/johncad /etc/nginx/sites-enabled/johncad
sudo rm -f /etc/nginx/sites-enabled/default
```

nginx won't load the config until the cert exists, so first comment out the
whole `listen 443` server block in `/etc/nginx/sites-available/johncad`, then:

```bash
sudo nginx -t && sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/certbot -d rhsprint.crabdance.com
```

Un-comment the 443 block, then `sudo nginx -t && sudo systemctl reload nginx`.
Certbot installs a renewal timer; add a reload hook so nginx picks up renewed
certs: `echo 'systemctl reload nginx' | sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh && sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh`.
Finally change `HTTPS_ENABLED=False` to `True` in `.env` (no inline comments in `.env` — systemd doesn't strip them) and `sudo systemctl restart johncad`.
The site is at `https://rhsprint.crabdance.com/`. Uploads up to 512MB are allowed
(`client_max_body_size` in the nginx config).

**9. Finish up:** log into `/admin/`, then set up the printer controller's
API token (next section). After pulling updates later:

```bash
git pull && source .venv/bin/activate && pip install -r requirements.txt
python manage.py migrate && python manage.py collectstatic --noinput
sudo systemctl restart johncad
```

### Serving through an frp tunnel (no inbound access at the school)

If the school network can't accept inbound connections, run `frps` + Caddy on a
home Pi and `frpc` on the school Pi. Example configs and step-by-step notes are in
[`deploy/frp/`](deploy/frp/README.md).

### Printer controller setup

The printer controller (an Arduino UNO R4 WiFi, not part of this repo) needs
credentials to talk to the API:

1. In `/admin/`, create a dedicated user for it (e.g. `printer-service`) and
   tick **Staff status** — the API rejects non-staff users. Don't give it
   any other permissions.
2. In the admin's **Tokens** section, create a token for that user.
3. Give the controller that key; it authenticates with
   `Authorization: Token <key>` on every request.

Full endpoint documentation, example Arduino C firmware, and hardware/library
notes are in `PRINTER_CONTROLLER_API.md`.

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
python manage.py collectstatic          # gather static files (needed in prod, entrypoint.sh does this automatically)
python manage.py check                  # system checks, catches config errors
python manage.py shell                  # interactive Python shell with models loaded
```

Docker Compose:

```bash
docker compose up -d db          # just Postgres, for local host-venv dev
docker compose up -d --build     # full stack: db + web (gunicorn) + nginx
docker compose logs -f web       # tail the Django/gunicorn container's logs
docker compose exec web python manage.py <command>   # run a management command inside the container
docker compose down              # stop everything (data persists in named volumes)
```

Useful one-offs via `manage.py shell -c "..."`:

```bash
# Create a printer-service token user
python manage.py shell -c "
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
u, _ = User.objects.get_or_create(username='printer-service', defaults={'is_staff': True})
t, _ = Token.objects.get_or_create(user=u)
print(t.key)
"
```

## Program layout

Three Django apps, split by feature, plus one project-level shared template:

```
JohnCad/
├── cms/                     # Django project (settings, root urls, wsgi/asgi)
│   └── settings.py          # all configuration, env-driven (see .env.example)
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
│   ├── views.py                        # attachment_export, queue
│   ├── urls.py                           # attachments/<id>/export/, queue/
│   ├── api_views.py, api_urls.py           # printer controller endpoints, mounted at /api/
│   ├── migrations/                           # schema history
│   └── templates/jobs/                         # queue.html
├── static/css/site.css        # the site's one stylesheet
├── nginx/nginx.conf            # reverse-proxy config for the Docker stack
├── Dockerfile                   # web image: Python + gunicorn
├── docker-compose.yml             # db + web + nginx services
├── entrypoint.sh                    # container startup: migrate, collectstatic, then exec gunicorn
├── requirements.txt                  # Python dependencies
├── .env.example                       # documented list of all env vars
├── CLAUDE.md                           # architecture notes, current status, open items
└── PRINTER_CONTROLLER_API.md            # printer controller API contract
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
| `status` | One of `pending`/`approved`/`queued`/`working`/`finished`/`rejected` — see status table below |
| `gcode` | Sliced output, populated by `slicing.slice_to_gcode()` once a job is approved and slicing succeeds. Blank until then. |
| `created_at` | Set when the Student clicks Export |
| `slicing_started_at` | Set while the slice worker is slicing the job; blank otherwise |
| `slicing_error` | Why the last slice failed; blank if none. Cleared on retry |
| `approved_at` | Set when an admin approves; drives FIFO ordering of the queue |
| `working_at` | Set by the printer controller API when it starts exporting to a microSD card |
| `finished_at` | Set by the printer controller API when the microSD card is returned |

#### Job status lifecycle

| Status | Set by | Meaning |
|---|---|---|
| `pending` | Student (Export click) | Awaiting admin review |
| `approved` | Admin / teacher | Signed off; waiting for the `slice_worker` to slice it (shown as Waiting to slice / Slicing… / Slicing failed) |
| `queued` | System, automatically | Slicing succeeded, G-code ready, sitting in the active print queue (FIFO by `approved_at`) |
| `working` | Printer controller, via API | G-code has been exported to a microSD card |
| `finished` | Printer controller, via API | The microSD card came back after printing |
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

- **Approve selected jobs** — moves `pending → approved`, stamps
  `approved_at`, and immediately attempts slicing. On success the job
  auto-advances to `queued`; on failure it's left in `approved` with a
  message explaining why (see status table above).
- **Reject selected jobs** — moves `pending → rejected`.

### Slicing (`jobs/slicing.py`)

The `slice_worker` management command (`jobs/management/commands/`, logic in
`jobs/services.py`) polls for `approved` jobs without G-code, claims the oldest
one atomically, and calls `slice_to_gcode`. Failures are stored on the job, so
a failing job isn't retried in a loop until a teacher clicks Retry. Claims left
by a crashed worker are released after `ORCA_SLICER_TIMEOUT` + 60s.

`slice_to_gcode(job)` invokes OrcaSlicer as a subprocess
(`<ORCA_SLICER_CMD> --slice 0 --outputdir <tmp> --load-settings <settings>
[--load-filaments <filaments>] [--datadir <dir>] <stl>`), picks up the
generated `.gcode` from the output dir into `job.gcode`, and advances the job
to `queued`. No shell interpolation (`shell=True`) is used. Configurable via
the `ORCA_SLICER_*` env vars above.

### Public print queue (`/queue/`)

Any logged-in Student (not just admins) can see what's currently `queued` or
`working` — attachment name, uploader's username, and status. Read-only;
there's no way for a Student to reorder or cancel jobs from here.

### Printer controller API (`/api/...`)

Token-authenticated (DRF `TokenAuthentication`), built for an Arduino UNO R4
WiFi at the printers to poll and pull jobs — see `PRINTER_CONTROLLER_API.md`
for the full contract with examples. In short:

- `GET /api/jobs/queue/` — what's ready to print, FIFO
- `GET /api/jobs/<id>/gcode/` — download a job's sliced G-code
- `POST /api/jobs/<id>/working/` — mark a job as being written to a microSD card
- `POST /api/jobs/<id>/finished/` — mark a job finished once the card returns

Invalid state transitions (e.g. `finished` before `working`) return `409`
rather than silently succeeding.

### Deployment

Dockerized for a Raspberry Pi: `Dockerfile` (Python + gunicorn,
running as a non-root user), `docker-compose.yml` (Postgres + the Django app
+ nginx reverse proxy), `entrypoint.sh` (runs migrations and `collectstatic`
before starting gunicorn). Static files are served via WhiteNoise from
within the Django process — no separate static file server needed.

The `web` image no longer bundles a slicer; slicing runs against the host's
OrcaSlicer Flatpak (see the OrcaSlicer section), so run Django on the host
for production slicing.
