# JohnCad

Django CMS for a 3D-printing pipeline: Students upload `.stl` files,
request prints, teachers approve them, OrcaSlicer slices them to
`.gcode.3mf`, and a **print agent** hands them to Bambu printers through a
self-hosted **Bambuddy**. Django never talks to printers or to Bambuddy.

**Production runs on two Raspberry Pis** (see `README.md` for the diagram):
- the **site Pi** (a Pi 5, at home): gunicorn + `slice_worker` (OrcaSlicer
  Flatpak) on the host, Postgres in Docker, Caddy for HTTPS,
  `BAMBUDDY_VIA_AGENT=True`. Installed by `scripts/setup-site.sh`.
- the **printer Pi** (a Pi 3, at the school): Bambuddy in Docker and the
  `johncad-agent` service (`agent/`). It only makes outbound HTTPS calls to
  the site, so the school network needs no inbound access. There is no tunnel
  or VPN (frp and Tailscale are not used).

`docs/bambuddy.md` has how JohnCad uses Bambuddy and its API details;
`docs/operations.md` is the runbook.

## Current state (implemented)

- Django 6.1 + Postgres, native `auth.User` (referred to as "Students" in
  product language, even though the underlying model is still
  `django.contrib.auth.User`).
- Three Django apps, split by feature (plus the standalone `agent/`): `accounts` (Student identity, classes,
  signup codes; depends on no other app),
  `attachments` (raw `.stl` upload), `jobs` (the print pipeline + Bambuddy
  dispatch and webhook). Dependency direction is one-way — `jobs` imports from `attachments`
  (the `Job.attachment` FK, and the `attachment_export` view), never the
  reverse, aside from `attachments`'s home view doing one read-only `Job`
  query for its "My Jobs" table. Shared page layout lives at project-level
  `templates/base.html`, not inside any single app.
- `Attachment` model (`attachments/models.py`): a Student's uploaded `.stl`
  file, `FileField`-backed, stored under `MEDIA_ROOT` (private — never
  mounted at a public URL, only reachable through authenticated views).
- `Printer` model (`jobs/models.py`, managed in `/admin/`): name, Bambuddy
  printer id, the OrcaSlicer preset paths for that printer's model, and an
  `is_active` flag. A teacher picks one on `/review/` when approving
  (required once any active printer exists); it sets `Job.printer` (nullable,
  `PROTECT`). The printer decides the slicer presets (`jobs/slicing.py`
  `_presets_for`: a printer's own presets replace `ORCA_SLICER_*` as a pair,
  never mixed), the Bambuddy printer the job is enqueued on, and which
  "Plate cleared, release next" button (one per printer on `/queue/`) starts
  it. Jobs with no printer (older jobs, or no printers set up) use the
  `.env` settings, so single-printer setups keep working.
- `Job` model (`jobs/models.py`): one specific request to print a
  Student's `Attachment`. Status lifecycle: `pending -> approved -> queued ->
  working -> finished`, plus `rejected` (terminal, from `pending`) and
  `failed` (print failed in Bambuddy; teacher Retry re-queues it). See table
  below for who/what sets each status.
- Session-based login/logout (`accounts` app). **Code-gated signup** at
  `/signup/`: a teacher creates a `Classroom` ("Class" in the UI) at
  `/classes/` and generates a `SignupCode` (`accounts/models.py`; 8 chars,
  multi-use, expires 24h after creation, revocable). Signing up with a valid
  code creates a non-staff account (username, first/last name, email,
  password) and adds it to that class; accounts outlive the code. Logged-in
  students join more classes by entering a code on `/classes/`. A class can
  have several teachers (any teacher of it manages roster, codes and
  co-teachers; can't remove the last one); superusers manage all classes.
  Permission helpers (`staff_required`, `classes_visible_to`) live in
  `accounts/permissions.py`. Attachment list page
  (`attachments` app, `/`) lets a Student upload, download their own files,
  and click **Export** to create a `Job` (view lives in `jobs/views.py` since
  it creates a `Job`, even though its URL is `attachments/<id>/export/`); the
  same page also shows the Student's own job history/status.
- Each `Job` has a nullable `classroom` FK: on Export the Student picks one
  of their classes (auto-picked if they have exactly one; `None` if they have
  none). Teachers (`is_staff`) approve/reject jobs at `/review/`
  (`jobs/views.py`, logic in `jobs/services.py`; newest first), scoped by
  `jobs_visible_to()` to jobs submitted to classes they teach plus their own;
  superusers see everything, including classless jobs. `?classroom=<id>`
  (or `none`, superusers) filters the list. Staff attachment downloads are
  scoped the same way. Django `/admin/` is
  superuser-only (`cms/admin.py` `SuperuserAdminSite`).
- Admin (`jobs/admin.py`) has **Approve selected** / **Reject selected** bulk
  actions on `Job`, at `/admin/jobs/job/`. Approving only sets `approved_at`;
  slicing runs asynchronously in the `slice_worker` management command
  (`jobs/management/commands/slice_worker.py`, claim/slice logic in
  `jobs/services.py`; run as the `johncad-slicer` systemd service on the
  site Pi). On success the job auto-advances to `queued`; on failure it stays in
  `approved` with `Job.slicing_error` set (shown on `/review/`; Retry clears
  it). `AttachmentAdmin` lives in
  `attachments/admin.py`; the `User` re-registration lives in
  `accounts/admin.py`.
- **Slicing** (`jobs/slicing.py`): `slice_to_3mf(job)` shells out to
  the OrcaSlicer CLI (`subprocess.run`, no `shell=True`, `--export-3mf`) to
  convert the attachment's `.stl` into a `.gcode.3mf` in `job.sliced_file`
  (renamed from `gcode` in migration 0004). OrcaSlicer is a **Flatpak on the site Pi**
  (`ORCA_SLICER_CMD`, default `flatpak run com.orcaslicer.OrcaSlicer`) and
  runs on the host, which is why the site is not run in Docker. Config via settings/env:
  `ORCA_SLICER_SETTINGS` (machine+process preset JSONs, `;`-separated,
  required), `ORCA_SLICER_FILAMENTS`, `ORCA_SLICER_DATADIR`,
  `ORCA_SLICER_TIMEOUT` (300s). Scratch output goes in
  `MEDIA_ROOT/slicing_tmp` (not `/tmp`) because the Flatpak sandbox has a
  private `/tmp`; the sandbox needs `flatpak override --filesystem=` for
  `MEDIA_ROOT` and the preset dir. Raises `SlicerNotConfigured` if settings are
  blank or the command isn't found, `SlicingFailed` (with captured
  stderr) on non-zero exit/timeout/no `.3mf` output — both caught by the
  slice worker and stored in `slicing_error`. In production use.
- Public **`/queue/`** page (`jobs` app; any logged-in Student, not just
  admins) lists `queued`/`working` jobs — attachment name, username, status.
- Staff see a **Plate cleared, release next** button on `/queue/`. With a
  print agent it records `Job.release_requested_at` for the agent to carry out
  (see below); in direct mode it asks Bambuddy to start the oldest staged job's
  queue item (`dispatch.release_next` -> `POST /queue/<id>/start`).
- **Direct-mode dispatch** (optional, `BAMBUDDY_VIA_AGENT=False`, **not used
  in production**; `jobs/dispatch.py`, `dispatch_worker` command, the
  `johncad-dispatcher` unit that `setup-site.sh` does not install): claims sliced `queued` jobs with no
  `bambuddy_queue_id`, uploads the file as `johncad-job-<id>.gcode.3mf`,
  enqueues it staged ("Queue Only") on the job's printer
  (`Job.printer.bambuddy_printer_id`, else the `BAMBUDDY_PRINTER_ID` fallback), stores
  `bambuddy_file_id`/`bambuddy_queue_id`/`dispatched_at`. Errors go in
  `dispatch_error`, shown on `/review/` with a Retry button. The worker also
  polls each in-flight job's queue item (`GET /queue/<id>`) every 60s as a
  fallback for missed webhooks.
- **Print agent** (`BAMBUDDY_VIA_AGENT=True`, `agent/README.md`): the way jobs
  reach the printers in production (site Pi 5 at home, printer Pi 3 and
  printers at school behind a network that only allows outbound HTTPS).
  `agent/johncad_agent.py` (no
  Django; needs `requests`, imports `jobs/bambuddy_client.py` from a repo
  checkout; systemd `johncad-agent`, config `/etc/johncad-agent.env`) runs
  next to Bambuddy and polls `/agent-api/v1/` (`jobs/agent_api.py`, state
  changes in `jobs/dispatch.py` `agent_*`): claims sliced jobs (`Job.agent`,
  claim kept alive by each file request, lapses after `AGENT_CLAIM_TIMEOUT`),
  downloads with HTTP Range chunks + SHA-256, uploads/enqueues in Bambuddy,
  reports `dispatched`/`dispatch-failed`. Reports are saved to disk until
  delivered, so a job is never sent twice, and a lock allows one agent per
  state dir. "Release next" only sets `Job.release_requested_at`; the agent
  starts it and reports `released` or `release-failed` (`Job.release_error`,
  shown on `/queue/`). Print status comes from the agent reading Bambuddy's
  queue (`event`), not the webhook. `PrinterAgent` model (`/admin/`, or
  `manage.py create_agent_key`): key `jca_...` stored as SHA-256, revocable,
  optional printer scope. The API 404s unless agent mode is on, and the
  `dispatch_worker` idles in agent mode. Under `/agent-api/`, not `/api/`, so
  Caddy doesn't block it. Tested against a fake Bambuddy over real HTTP, and in
  production use at the school. CI also checks that an agent can connect to a
  freshly installed site (`agent/johncad_agent.py --check`).
- **Bambuddy client** (`jobs/bambuddy_client.py`, no Django imports, shared
  with the agent; `jobs/bambuddy.py` wraps it with settings and parses the
  webhook): every Bambuddy API detail lives here as constants, checked against Bambuddy's source (commit `98ecaf5`,
  2026-10-22) and in use against a live Bambuddy through the agent. Base `/api/v1`, key in
  `X-API-Key`; upload `POST /library/files/` (multipart `file`, returns
  `id`); enqueue `POST /queue/` with `library_file_id`, `printer_id`,
  `manual_start`; release `POST /queue/<id>/start`; queue item status
  `pending|printing|completed|failed|skipped|cancelled`. Key permissions:
  Manage library, Add to queue, Read status.
- **Webhook** (direct mode only, not used in production)
  `POST /api/bambuddy/webhook/` (`jobs/api_views.py`, plain
  Django view; DRF was removed with the Arduino API): shared secret
  (`BAMBUDDY_WEBHOOK_SECRET`, as `Authorization: Bearer` from Bambuddy's
  webhook "Auth header" setting, or `?secret=`). Bambuddy's generic payload
  is flat (`event`, `filename`, `printer`, `reason`, ...) with no
  printer/archive/queue ids, so jobs are matched by `johncad-job-<id>` in
  `filename` (Bambuddy strips `.gcode.3mf`). Maps
  `print_start -> working`, `print_complete -> finished`,
  `print_failed`/`print_stopped` -> `failed`; repeats are no-ops, invalid
  transitions 409. Local-only: the Caddyfile 404s `/api/`, and Bambuddy
  (host networking) posts to gunicorn on `127.0.0.1:8000`.
- Settings/env: `BAMBUDDY_VIA_AGENT` (True in production), `BAMBUDDY_STAGED`
  (default True, the agent follows it), `AGENT_CLAIM_TIMEOUT`,
  `AGENT_POLL_INTERVAL`. Direct mode only: `BAMBUDDY_URL`, `BAMBUDDY_API_KEY`,
  `BAMBUDDY_WEBHOOK_SECRET`, `BAMBUDDY_PRINTER_ID`, `BAMBUDDY_TIMEOUT`; with an
  agent these live in the printer Pi's `/etc/johncad-agent.env`.
- `docker-compose.yml` has two services used on different machines: `db`
  (Postgres 16, site Pi) and `bambuddy` (`ghcr.io/maziggy/bambuddy`, host
  networking, port 8080, AGPL-3.0; printer Pi). The site itself is not run in
  Docker (it needs the host's OrcaSlicer Flatpak), so there is no Dockerfile.

## Development and operations

- **Run the checks CI runs** before finishing a change: `ruff check .`,
  `python manage.py test` (works from a clean clone; SQLite via
  `DATABASE_ENGINE=sqlite` is fine locally, CI uses Postgres 16),
  `python manage.py makemigrations --check --dry-run`, and
  `shellcheck -x scripts/*.sh` if a script changed. Details:
  `CONTRIBUTING.md`.
- **CI** (`.github/workflows/ci.yml`): lint, Postgres tests with deprecation
  warnings as errors, `check --deploy`, `pip-audit`, and an install drill that
  runs `scripts/setup-site.sh` twice, checks an agent can connect to the site
  (`agent/johncad_agent.py --check`), then runs `scripts/restore-drill.sh`, on a
  clean Ubuntu runner.
- **Tests** use `cms/test_runner.py` (plain static storage, temporary
  `MEDIA_ROOT`). `jobs/test_agent.py` is skipped on Windows (`fcntl`).
- **Settings**: `DEBUG` defaults to `False`; without `DEBUG=True` the app
  won't start unless `SECRET_KEY` is set to a real value.
- **Install and operate** the site Pi with `scripts/`: `setup-site.sh`
  (one-command install, idempotent), `update.sh` (backup, pull, migrate, restart, check
  `/healthz/`), `backup.sh`/`restore.sh` (Postgres dump + `private_media`
  archive). systemd units live in `deploy/systemd/` (`johncad-dispatcher` is
  direct-mode only and not installed by default; `lib.sh` only restarts it where
  it is enabled). Caddy config example: `deploy/caddy/`. Runbook:
  `docs/operations.md`.
- **Migrations must keep the previous release working** (rollback is a code
  checkout, never a schema rollback): see `CONTRIBUTING.md`, "Database
  migrations".
- The scripts are **not yet run on a real Raspberry Pi** from scratch (the
  production Pis predate them); CI covers Ubuntu x86 without the OrcaSlicer
  Flatpak, Caddy or the printer Pi.

## Job status enum

| Status     | Set by                             | Meaning |
|------------|-------------------------------------|---------|
| `pending`  | Student (via Export click)          | Job created, awaiting admin review |
| `approved` | Admin                               | Admin signed off; slicing attempted here |
| `queued`   | System (auto, on slicing success)   | `.gcode.3mf` ready; dispatched to Bambuddy, then staged until staff release it |
| `working`  | Bambuddy webhook / poll             | Print started |
| `finished` | Bambuddy webhook / poll             | Print completed |
| `failed`   | Bambuddy webhook / poll             | Print failed or stopped; teacher Retry re-queues |
| `rejected` | Admin                               | Declined instead of approved; terminal |

## Still open / not implemented

- "Any X1C"-style automatic printer choice: teachers pick the printer by
  hand on `/review/` for now.
- Pin the Bambuddy image version on the printer Pi (`BAMBUDDY_VERSION`,
  currently `latest`).
- **Direct mode** (`dispatch_worker`, the webhook, `jobs/bambuddy.py`,
  `BAMBUDDY_URL`/`API_KEY`/`WEBHOOK_SECRET` on the site) is kept as an
  alternative but is not used or tested against a live Bambuddy. Remove it if
  it stays unused; the agent path does not depend on it, though `dispatch.py`
  is shared.
- No installer for the printer Pi yet (it is set up by hand following
  `agent/README.md`); HTTPS/Caddy setup on the site Pi is manual too.
- LICENSE file, and configurable domain settings (the examples use
  `print.example.org`).
