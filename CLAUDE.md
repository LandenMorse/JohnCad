# JohnCad

Django CMS for a 3D-printing pipeline: Students upload `.stl` files,
request prints, teachers approve them, OrcaSlicer slices them to
`.gcode.3mf`, and JohnCad dispatches them to Bambu printers through a
self-hosted **Bambuddy** on the same Raspberry Pi. Django never talks to
printers, only to Bambuddy's REST API; Bambuddy reports print status back via
a webhook. The old Arduino/microSD printer controller has been removed. Plan,
network setup (GL.iNet router + Tailscale subnet route to the school
printers) and the unverified Bambuddy API details: `docs/BAMBUDDY_PLAN.md`.

## Current state (implemented)

- Django 6.1 + Postgres, native `auth.User` (referred to as "Students" in
  product language, even though the underlying model is still
  `django.contrib.auth.User`).
- Three Django apps, split by feature: `accounts` (Student identity, classes,
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
  `jobs/services.py`; run as the `johncad-slicer` systemd service on the Pi
  host). On success the job auto-advances to `queued`; on failure it stays in
  `approved` with `Job.slicing_error` set (shown on `/review/`; Retry clears
  it). `AttachmentAdmin` lives in
  `attachments/admin.py`; the `User` re-registration lives in
  `accounts/admin.py`.
- **Slicing** (`jobs/slicing.py`): `slice_to_3mf(job)` shells out to
  the OrcaSlicer CLI (`subprocess.run`, no `shell=True`, `--export-3mf`) to
  convert the attachment's `.stl` into a `.gcode.3mf` in `job.sliced_file`
  (renamed from `gcode` in migration 0004). OrcaSlicer is a **Flatpak on the Pi**
  (`ORCA_SLICER_CMD`, default `flatpak run com.orcaslicer.OrcaSlicer`) and must
  run on the host, not in the `web` container. Config via settings/env:
  `ORCA_SLICER_SETTINGS` (machine+process preset JSONs, `;`-separated,
  required), `ORCA_SLICER_FILAMENTS`, `ORCA_SLICER_DATADIR`,
  `ORCA_SLICER_TIMEOUT` (300s). Scratch output goes in
  `MEDIA_ROOT/slicing_tmp` (not `/tmp`) because the Flatpak sandbox has a
  private `/tmp`; the sandbox needs `flatpak override --filesystem=` for
  `MEDIA_ROOT` and the preset dir. Raises `SlicerNotConfigured` if settings are
  blank or the command isn't found, `SlicingFailed` (with captured
  stderr) on non-zero exit/timeout/no `.3mf` output — both caught by the
  slice worker and stored in `slicing_error`. **Unverified on the Pi** (flags taken
  from `--help`, not yet run end-to-end).
- Public **`/queue/`** page (`jobs` app; any logged-in Student, not just
  admins) lists `queued`/`working` jobs — attachment name, username, status.
- Staff see a **Plate cleared, release next** button on `/queue/` that asks
  Bambuddy to start the oldest staged job's queue item
  (`dispatch.release_next` -> `POST /queue/<id>/start`).
- **Dispatch** (`jobs/dispatch.py`, `dispatch_worker` command, run as the
  `johncad-dispatcher` systemd service): claims sliced `queued` jobs with no
  `bambuddy_queue_id`, uploads the file as `johncad-job-<id>.gcode.3mf`,
  enqueues it staged ("Queue Only") on the job's printer
  (`Job.printer.bambuddy_printer_id`, else the `BAMBUDDY_PRINTER_ID` fallback), stores
  `bambuddy_file_id`/`bambuddy_queue_id`/`dispatched_at`. Errors go in
  `dispatch_error`, shown on `/review/` with a Retry button. The worker also
  polls each in-flight job's queue item (`GET /queue/<id>`) every 60s as a
  fallback for missed webhooks.
- **Bambuddy client** (`jobs/bambuddy.py`): every Bambuddy API detail lives
  here as constants, checked against Bambuddy's source (commit `98ecaf5`,
  2026-10-22), not yet against a running instance. Base `/api/v1`, key in
  `X-API-Key`; upload `POST /library/files/` (multipart `file`, returns
  `id`); enqueue `POST /queue/` with `library_file_id`, `printer_id`,
  `manual_start`; release `POST /queue/<id>/start`; queue item status
  `pending|printing|completed|failed|skipped|cancelled`. Key permissions:
  Manage library, Add to queue, Read status.
- **Webhook** `POST /api/bambuddy/webhook/` (`jobs/api_views.py`, plain
  Django view; DRF was removed with the Arduino API): shared secret
  (`BAMBUDDY_WEBHOOK_SECRET`, as `Authorization: Bearer` from Bambuddy's
  webhook "Auth header" setting, or `?secret=`). Bambuddy's generic payload
  is flat (`event`, `filename`, `printer`, `reason`, ...) with no
  printer/archive/queue ids, so jobs are matched by `johncad-job-<id>` in
  `filename` (Bambuddy strips `.gcode.3mf`). Maps
  `print_start -> working`, `print_complete -> finished`,
  `print_failed`/`print_stopped` -> `failed`; repeats are no-ops, invalid
  transitions 409. Local-only: both nginx configs 404 `/api/`, and Bambuddy
  (host networking) posts to gunicorn on `127.0.0.1:8000`.
- Settings/env: `BAMBUDDY_URL` (default `http://127.0.0.1:8080`), `BAMBUDDY_API_KEY`, `BAMBUDDY_WEBHOOK_SECRET`,
  `BAMBUDDY_PRINTER_ID`, `BAMBUDDY_STAGED` (default True), `BAMBUDDY_TIMEOUT`.
- Dockerized (Dockerfile, docker-compose with `db`/`web`/`nginx`/`bambuddy`,
  `entrypoint.sh`) for deployment to a Raspberry Pi hosting the site +
  Postgres + Bambuddy (`ghcr.io/maziggy/bambuddy`, port 8080, AGPL-3.0).

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

- **Run the Bambuddy calls against a live instance**: they're checked
  against Bambuddy's source only. Watch for the 409 that
  `POST /queue/<id>/start` returns when Bambuddy thinks the spool is short.
- **Hand-upload one `.gcode.3mf`** through Bambuddy's UI and print it, to
  confirm OrcaSlicer's CLI 3MF output works on the printer model.
- **Verify OrcaSlicer end-to-end on the Pi** (headless Flatpak, filesystem
  overrides, preset paths).
- "Any X1C"-style automatic printer choice: teachers pick the printer by
  hand on `/review/` for now.
- Pin the Bambuddy image version; pick one deployment path (the `web`
  container still can't slice).
- Repo cleanup before open-sourcing: LICENSE, quick-start, configurable
  domain settings (see `docs/BAMBUDDY_PLAN.md`).
