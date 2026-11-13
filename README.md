# JohnCad

A Django app for a small 3D-printing pipeline: Students upload `.stl` files,
request prints, a teacher reviews and approves them, OrcaSlicer converts the
approved files to `.gcode.3mf`, and JohnCad sends them to Bambu printers
through a self-hosted [Bambuddy](https://github.com/maziggy/bambuddy) on the
same Raspberry Pi. Django never talks to the printers directly.

## Quick start

**Running it for a class** (a Raspberry Pi with Raspberry Pi OS 64-bit):

```bash
git clone https://github.com/LandenMorse/JohnCad.git && cd JohnCad
scripts/setup-pi.sh
```

That installs everything, starts the site and sets up nightly backups. Then
follow [Raspberry Pi deployment](#raspberry-pi-deployment-production) for the
three things only you can do: a web address, your printers, and slicer
presets.

**Working on the code:** see [CONTRIBUTING.md](CONTRIBUTING.md). You can run
and test it on any computer in about five minutes, without a Pi or a printer.

## Documentation

| | |
|---|---|
| This README | Installing, configuring, and what each part of the program does |
| [docs/operations.md](docs/operations.md) | Updating, backups, restoring, rolling back, troubleshooting |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup, tests, CI, conventions, migration rules |
| [agent/README.md](agent/README.md) | Site at home, printers at school (print agent) |
| [deploy/frp/README.md](deploy/frp/README.md) | Serving through a tunnel when the school blocks incoming connections |
| [docs/BAMBUDDY_PLAN.md](docs/BAMBUDDY_PLAN.md) | Bambuddy plan, network setup and what is still unverified |
| [CLAUDE.md](CLAUDE.md) | Current implementation status and open items |

## Contents

- [Quick start](#quick-start)
- [Setup](#setup) (development)
- [Raspberry Pi deployment](#raspberry-pi-deployment-production)
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
| `CSRF_TRUSTED_ORIGINS` | Comma-separated origins, e.g. `https://rhsprint.crabdance.com` — needed once served through nginx on a real domain        |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | Postgres credentials                                                                                                      |
| `POSTGRES_HOST` | `localhost` for host-venv dev; `docker compose up` overrides this to `db` automatically for the containerized web service |
| `POSTGRES_PORT` | Default `5432`                                                                                                            |
| `ORCA_SLICER_CMD` | Command to launch OrcaSlicer, default `flatpak run com.orcaslicer.OrcaSlicer`                                             |
| `ORCA_SLICER_SETTINGS` | Semicolon-separated machine + process preset `.json` files (`--load-settings`); required — blank = slicer not configured  |
| `ORCA_SLICER_FILAMENTS` | Semicolon-separated filament preset `jm.json` file(s) (`--load-filaments`), optional                                      |
| `ORCA_SLICER_DATADIR` | Optional OrcaSlicer `--datadir`                                                                                           |
| `ORCA_SLICER_TIMEOUT` | Slicing timeout in seconds, default `300`                                                                                 |
| `BAMBUDDY_URL` | Bambuddy base URL, default `http://127.0.0.1:8080` (Bambuddy runs on the same host as Django)                              |
| `BAMBUDDY_API_KEY` | Bambuddy API key (Manage library, Add to queue, Read status); blank = dispatch not configured                        |
| `BAMBUDDY_WEBHOOK_SECRET` | Shared secret Bambuddy's webhook must send; blank rejects all webhooks                                         |
| `BAMBUDDY_PRINTER_ID` | Fallback Bambuddy printer id for jobs with no printer chosen (single-printer setups; otherwise add Printers in `/admin/`) |
| `BAMBUDDY_STAGED` | `True` (default): jobs wait in Bambuddy until staff release them after clearing the plate                                |
| `BAMBUDDY_TIMEOUT` | Bambuddy request timeout in seconds, default `60`                                                                     |
| `BAMBUDDY_VIA_AGENT` | `True` when Bambuddy is on another network and a print agent pulls jobs from `/agent-api/` (see [`agent/`](agent/README.md)); default `False` |
| `AGENT_CLAIM_TIMEOUT` | Seconds before a job an agent stopped downloading is offered again, default `900`                                  |
| `AGENT_POLL_INTERVAL` | Seconds between agent check-ins, sent to agents, default `15`                                                      |
| `BAMBUDDY_VERSION` / `BAMBUDDY_PORT` | Image tag and port for the compose `bambuddy` service                                                       |

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
the OrcaSlicer Flatpak; **Postgres runs in Docker**. Day-to-day running
(updates, backups, restoring, troubleshooting) is in
[docs/operations.md](docs/operations.md).

**1. Install.** On the Pi (Raspberry Pi OS 64-bit; Python 3.13 is required,
which Raspberry Pi OS 13 "Trixie" includes):

```bash
sudo apt install -y git
git clone https://github.com/LandenMorse/JohnCad.git && cd JohnCad
scripts/setup-pi.sh
```

The script is safe to run again, and does the following:

| Step | What happens |
|---|---|
| Packages | Installs Docker, Python venv support, Flatpak and the OrcaSlicer Flatpak (`--no-orca` skips OrcaSlicer) |
| Python | Creates `.venv/` and installs `requirements.txt` |
| Settings | Writes `.env` from `.env.example` with a new `SECRET_KEY`, `POSTGRES_PASSWORD` and `BAMBUDDY_WEBHOOK_SECRET`, `DEBUG=False`, and this Pi's names in `ALLOWED_HOSTS`. An existing `.env` is never touched |
| Database | Starts Postgres (`docker compose up -d db`), runs `migrate` and `collectstatic` |
| Services | Installs the units in [`deploy/systemd/`](deploy/systemd/): `johncad` (gunicorn on `127.0.0.1:8000`), `johncad-slicer`, `johncad-dispatcher`, and a nightly `johncad-backup` timer |
| Admin login | Asks for a username and password if no admin exists yet |

When it finishes, the site answers on the Pi itself at
`http://127.0.0.1:8000/`. Gunicorn listens on localhost only; the next step
makes it reachable. Static files are served by WhiteNoise. The compose
`web`/`nginx` services and `nginx/nginx.conf` are not used in this setup (the
`web` container can't reach the host's Flatpak).

Without `johncad-slicer` running, approved jobs sit at "Waiting to slice";
`johncad-dispatcher` sends sliced jobs to Bambuddy and, every 60s, polls
Bambuddy's queue in case a webhook was missed. One-off runs without the
services: `python manage.py slice_worker --once`,
`python manage.py dispatch_worker --once`.

**2. A web address: nginx + HTTPS.** Requires a real domain name pointing at the Pi (A/AAAA
record) with ports 80 and 443 forwarded to it from your router. The config
returns 404 for `/api/`: the Bambuddy webhook is only reachable on the Pi
itself.

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
Add the domain to `ALLOWED_HOSTS` and set
`CSRF_TRUSTED_ORIGINS=https://<your-domain>` in `.env` as well.

If the school network can't accept incoming connections, use
[an frp tunnel](#serving-through-an-frp-tunnel-no-inbound-access-at-the-school)
or the [print agent](#site-at-home-printers-at-school-print-agent) instead of
this step.

**3. Printers.** Follow [Bambuddy setup](#bambuddy-setup).

**4. Slicer presets.** Put the Bambu machine/process/filament preset `.json`
files somewhere on the Pi (e.g. `/home/<user>/orca/`) and let the OrcaSlicer
sandbox read them (the setup script already did this for `private_media/`):

```bash
flatpak override --user --filesystem=/home/<user>/orca:ro com.orcaslicer.OrcaSlicer
```

Enter the paths on each Printer in `/admin/`, or for a single-printer setup
in `.env`:

```
ORCA_SLICER_SETTINGS=/home/<user>/orca/machine.json;/home/<user>/orca/process.json
ORCA_SLICER_FILAMENTS=/home/<user>/orca/filament.json
```

After editing `.env`, run
`sudo systemctl restart johncad johncad-slicer johncad-dispatcher`. Don't put
comments on the same line as a value in `.env`: systemd doesn't strip them.

Smoke-test slicing before relying on it:

```bash
mkdir -p private_media/slicing_tmp
flatpak run com.orcaslicer.OrcaSlicer --slice 0 --outputdir $PWD/private_media/slicing_tmp   --load-settings "/home/<user>/orca/machine.json;/home/<user>/orca/process.json" some_model.stl
```

It should write a `.gcode` file. Add `--export-3mf out.gcode.3mf` to check
the `.gcode.3mf` that JohnCad actually sends to Bambuddy. If it fails for lack of a display, try
`ORCA_SLICER_CMD=xvfb-run -a flatpak run com.orcaslicer.OrcaSlicer` in `.env`
(`sudo apt install xvfb`). This path is untested on real hardware.

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

cp .env.example .env     # then set SECRET_KEY, DEBUG=False, ALLOWED_HOSTS,
                         # POSTGRES_PASSWORD (before the first "docker compose
                         # up": Postgres only reads it when it creates its volume)

docker compose up -d db
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py createsuperuser
flatpak override --user --filesystem=$PWD/private_media com.orcaslicer.OrcaSlicer

# Services: replace @USER@ and @DIR@ in each file under deploy/systemd/,
# copy them to /etc/systemd/system/, then:
sudo systemctl daemon-reload
sudo systemctl enable --now johncad johncad-slicer johncad-dispatcher johncad-backup.timer
```

</details>

### Serving through an frp tunnel (no inbound access at the school)

If the school network can't accept inbound connections, run `frps` + Caddy on a
home Pi and `frpc` on the school Pi. Example configs and step-by-step notes are in
[`deploy/frp/`](deploy/frp/README.md).

### Site at home, printers at school (print agent)

If the site is hosted away from the printers (e.g. a Pi at home) and the
school network only allows outbound HTTPS, set `BAMBUDDY_VIA_AGENT=True` and
run the print agent next to Bambuddy at school. It checks in over port 443,
downloads jobs in resumable chunks and reports status back. No tunnel and no
inbound access needed. Setup: [`agent/README.md`](agent/README.md).

### Bambuddy setup

Network, printer and school-side steps are in `docs/BAMBUDDY_PLAN.md`. On the
Pi:

1. `docker compose up -d bambuddy`, then open `http://<pi>:8080`, add the
   printers (LAN Mode on) and **hand-upload and print one `.gcode.3mf`**
   before going further.
2. In Bambuddy (Settings > API keys), create a key with **Manage library**,
   **Add to queue** and **Read status**. Put it in `BAMBUDDY_API_KEY`.
   `BAMBUDDY_URL` defaults to `http://127.0.0.1:8080`.
   **Printers:** for each printer, add a **Printer** in JohnCad's `/admin/`
   (superuser): a name teachers will see, its Bambuddy id (shown in
   `GET /api/v1/printers/`), and the OrcaSlicer machine + process preset
   `.json` paths (`;`-separated) for that printer's model, plus filament
   presets if needed. Teachers then choose the printer when approving a job
   on `/review/`; it decides the slicer presets and where the job is sent.
   The job's print is released per printer on `/queue/`. Until you add a
   printer, jobs use `BAMBUDDY_PRINTER_ID` and `ORCA_SLICER_SETTINGS` from
   `.env` instead (single-printer setup).
3. Set `BAMBUDDY_WEBHOOK_SECRET` to a random string. In Bambuddy's
   Settings > Notifications, add a **Webhook** provider: URL
   `http://127.0.0.1:8000/api/bambuddy/webhook/`, payload format
   **generic**, **Auth header** = the secret, and turn on the print start,
   complete, failed and stopped events. `ALLOWED_HOSTS` must include
   `127.0.0.1`.
4. Use Bambuddy's API browser (`http://<pi>:8080/docs`) to sanity-check the
   calls in `jobs/bambuddy.py` against the version you run.
5. If you are upgrading from the Arduino setup, delete the old
   `printer-service` user in `/admin/`; its token no longer works.

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
# Send waiting jobs to Bambuddy and poll once, without the service
python manage.py dispatch_worker --once

# Create a print agent and print its key (shown once); --rotate replaces it
python manage.py create_agent_key school-pi
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
│   ├── bambuddy.py                     # Django wrapper for it (settings) + webhook parsing
│   ├── dispatch.py                     # dispatch, status sync, release-next, print agent state changes
│   ├── agent_api.py, agent_urls.py     # print agent API, mounted at /agent-api/v1/
│   ├── management/commands/            # slice_worker, dispatch_worker, create_agent_key
│   ├── views.py                        # attachment_export, queue, review
│   ├── urls.py                           # attachments/<id>/export/, queue/, review/
│   ├── api_views.py, api_urls.py           # Bambuddy webhook, mounted at /api/
│   ├── migrations/                           # schema history
│   └── templates/jobs/                         # queue.html
├── agent/                      # print agent for the school Pi (johncad_agent.py, systemd unit, README)
├── scripts/                    # setup-pi.sh, update.sh, backup.sh, restore.sh, restore-drill.sh
├── deploy/systemd/             # service and backup-timer units that setup-pi.sh installs
├── deploy/frp/                 # tunnel example configs
├── .github/                    # CI workflow, Dependabot, pull request template
├── static/css/site.css        # the site's one stylesheet
├── nginx/nginx.conf            # reverse-proxy config for the Docker stack
├── Dockerfile                   # web image: Python + gunicorn
├── docker-compose.yml             # db + web + nginx + bambuddy services
├── entrypoint.sh                    # container startup: migrate, collectstatic, then exec gunicorn
├── requirements.txt                  # Python dependencies
├── requirements-dev.txt              # ruff and pip-audit, for development and CI
├── .env.example                       # documented list of all env vars
├── CLAUDE.md                           # architecture notes, current status, open items
├── CONTRIBUTING.md                     # development setup, tests, CI, conventions
├── docs/operations.md                  # updating, backups, restoring, troubleshooting
└── docs/BAMBUDDY_PLAN.md                # Bambuddy plan, network setup, unverified items
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
| `bambuddy_file_id` / `bambuddy_queue_id` | Bambuddy's ids for the uploaded file and the queue item; set by the dispatch worker |
| `dispatch_started_at` | Set while the dispatch worker is sending the job |
| `dispatch_error` | Why the last dispatch or print failed; blank if none. Cleared on retry |
| `dispatched_at` | When the job reached Bambuddy's queue |
| `released_at` | When staff released it after clearing the plate |
| `working_at` / `finished_at` / `failed_at` | Set from Bambuddy's webhook (or the polling fallback) |

#### Job status lifecycle

| Status | Set by | Meaning |
|---|---|---|
| `pending` | Student (Export click) | Awaiting admin review |
| `approved` | Admin / teacher | Signed off; waiting for the `slice_worker` to slice it (shown as Waiting to slice / Slicing… / Slicing failed) |
| `queued` | System, automatically | Sliced; being sent to Bambuddy, then waiting there for the plate to be cleared (FIFO by `approved_at`) |
| `working` | Bambuddy webhook | The print started |
| `finished` | Bambuddy webhook | The print completed |
| `failed` | Bambuddy webhook | The print failed or was stopped; a teacher can Retry it |
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

### Dispatch to Bambuddy

The `dispatch_worker` command (logic in `jobs/dispatch.py`, Bambuddy calls in
`jobs/bambuddy.py`) claims the oldest sliced `queued` job, uploads its
`.gcode.3mf` as `johncad-job-<id>.gcode.3mf`, and adds it to the printer's
Bambuddy queue as a staged item. Failures are stored on the job and shown on
`/review/` with a **Retry** button (which reuses the upload); a failed print
can be retried the same way. Staff see **Plate cleared, release next** on
`/queue/`, which starts the oldest staged job's Bambuddy queue item
(`POST /api/v1/queue/<id>/start`).

### Print agent (`/agent-api/v1/`, `BAMBUDDY_VIA_AGENT=True`)

When Bambuddy is on another network, the `dispatch_worker` isn't used: a
print agent next to Bambuddy (`agent/johncad_agent.py`, run as a systemd
service) polls the site over HTTPS, claims sliced jobs, downloads them with
HTTP Range requests (resumable, SHA-256 checked), uploads and enqueues them in
its Bambuddy, and reports the queue item back. **Plate cleared, release next**
then only records a request, which the agent carries out on its next check-in
(a failure, such as a short spool, shows on `/queue/`). The agent reports
print status from Bambuddy's queue, so no webhook is needed. Agents
authenticate with a key managed in `/admin/` > Printer agents (stored hashed,
revocable, optionally limited to some printers). API and setup:
[`agent/README.md`](agent/README.md).

### Bambuddy webhook (`POST /api/bambuddy/webhook/`)

Bambuddy reports print events here (its "generic" webhook payload). It needs
`BAMBUDDY_WEBHOOK_SECRET` as `Authorization: Bearer <secret>` (Bambuddy's
"Auth header" setting) or `?secret=`, and is blocked in nginx, so only
Bambuddy on the Pi reaches it. Jobs are matched by the `johncad-job-<id>`
name Bambuddy reports as `filename`. `print_start` moves a job to `working`,
`print_complete` to `finished`, `print_failed`/`print_stopped` to `failed`.
Repeats are no-ops, other events are ignored, and invalid transitions (e.g.
complete before start) return `409`. The dispatch worker's 60s poll of each
job's Bambuddy queue item catches up on anything the webhook missed.

### Deployment

Production runs on the Pi host, installed by `scripts/setup-pi.sh` (see
[Raspberry Pi deployment](#raspberry-pi-deployment-production)); every change
is checked by CI, including a full install and a backup-and-restore drill
(see [CONTRIBUTING.md](CONTRIBUTING.md#what-ci-checks)).

Also Dockerized: `Dockerfile` (Python + gunicorn,
running as a non-root user), `docker-compose.yml` (Postgres + the Django app
+ nginx reverse proxy + Bambuddy), `entrypoint.sh` (runs migrations and `collectstatic`
before starting gunicorn). Static files are served via WhiteNoise from
within the Django process — no separate static file server needed.

The `web` image no longer bundles a slicer; slicing runs against the host's
OrcaSlicer Flatpak (see the OrcaSlicer section), so run Django on the host
for production slicing.
