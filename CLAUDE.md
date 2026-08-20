# JohnCad

Django CMS for a 3D-printing pipeline: Students upload `.stl` files, get them
sliced into G-code, request prints, and an Arduino UNO R4 WiFi controller at
the printers fulfills approved print jobs via microSD card transfer.

Two different boards show up in this project — don't conflate them: the
**JohnCad server** (Django + Postgres) runs on a Raspberry Pi; the
**printer controller** at the printers is a separate Arduino UNO R4 WiFi,
not a Pi. See `PRINTER_CONTROLLER_API.md` for the controller's firmware-side
API contract.

## Current state (implemented)

- Django 6.1 + Postgres, native `auth.User` (referred to as "Students" in
  product language, even though the underlying model is still
  `django.contrib.auth.User`).
- Three Django apps, split by feature: `accounts` (Student identity),
  `attachments` (raw `.stl` upload), `jobs` (the print pipeline + printer
  controller API). Dependency direction is one-way — `jobs` imports from `attachments`
  (the `Job.attachment` FK, and the `attachment_export` view), never the
  reverse, aside from `attachments`'s home view doing one read-only `Job`
  query for its "My Jobs" table. Shared page layout lives at project-level
  `templates/base.html`, not inside any single app.
- `Attachment` model (`attachments/models.py`): a Student's uploaded `.stl`
  file, `FileField`-backed, stored under `MEDIA_ROOT` (private — never
  mounted at a public URL, only reachable through authenticated views).
- `Job` model (`jobs/models.py`): one specific request to print a
  Student's `Attachment`. Status lifecycle: `pending -> approved -> queued ->
  working -> finished`, plus `rejected` (terminal, from `pending`). See table
  below for who/what sets each status.
- Session-based signup/login/logout (`accounts` app). Attachment list page
  (`attachments` app, `/`) lets a Student upload, download their own files,
  and click **Export** to create a `Job` (view lives in `jobs/views.py` since
  it creates a `Job`, even though its URL is `attachments/<id>/export/`); the
  same page also shows the Student's own job history/status.
- Admin (`jobs/admin.py`) has **Approve selected** / **Reject selected** bulk
  actions on `Job`, at `/admin/jobs/job/`. Approving sets `approved_at`,
  attempts slicing (see below), and auto-advances to `queued` on success — or
  leaves the job parked in `approved` with an admin-visible warning/error if
  slicing isn't configured or fails. `AttachmentAdmin` lives in
  `attachments/admin.py`; the `User` re-registration lives in
  `accounts/admin.py`.
- **Slicing** (`jobs/slicing.py`): `slice_to_gcode(job)` shells out to
  the PrusaSlicer CLI (`subprocess.run`, no `shell=True`) to convert the
  attachment's `.stl` into `job.gcode`. Configurable via settings/env:
  `PRUSA_SLICER_BIN` (default `prusa-slicer` on `PATH`), `PRUSA_SLICER_PROFILE`
  (optional `.ini` config bundle, currently unset — no profile exported yet),
  `PRUSA_SLICER_TIMEOUT` (default 300s). Raises `SlicerNotConfigured` if the
  binary isn't found, `SlicingFailed` (with captured stderr) on a non-zero
  exit/timeout — both are caught and surfaced by the admin approve action
  rather than crashing it.
  - **Unverified on real hardware, and currently broken even in local Docker
    builds**: the `Dockerfile`'s PrusaSlicer install step (downloads a GitHub
    release AppImage, picked by `TARGETARCH`) fails outright (`curl` exit 22
    — the release URL is wrong/guessed and 404s). This predates the app
    split and hasn't been fixed yet. PrusaSlicer's official Linux builds are
    primarily x86_64; the exact arm64 release asset/URL and whether
    `--export-gcode` runs headless (no display) on actual Raspberry Pi
    hardware are both still unverified. Needs real investigation before the
    Docker deployment path is usable.
- Public **`/queue/`** page (`jobs` app; any logged-in Student, not just
  admins) lists `queued`/`working` jobs — attachment name, username, status.
- DRF token-authenticated printer controller API (`jobs/api_urls.py`,
  `jobs/api_views.py`), built around `Job`, mounted at `/api/`:
  - `GET /api/jobs/queue/` — `queued` jobs, FIFO by `approved_at`.
  - `GET /api/jobs/<id>/gcode/` — streams the job's G-code.
  - `POST /api/jobs/<id>/working/` — `queued -> working` (409 if not queued).
  - `POST /api/jobs/<id>/finished/` — `working -> finished` (409 if not working).
  Full contract + example requests/polling loop in `PRINTER_CONTROLLER_API.md`.
- Dockerized (Dockerfile, docker-compose with `db`/`web`/`nginx`,
  `entrypoint.sh`) for deployment to a Raspberry Pi hosting the site +
  Postgres.

## Job status enum

| Status     | Set by                             | Meaning |
|------------|-------------------------------------|---------|
| `pending`  | Student (via Export click)          | Job created, awaiting admin review |
| `approved` | Admin                               | Admin signed off; slicing attempted here |
| `queued`   | System (auto, on slicing success)   | Gcode ready, FIFO in the active print queue, visible on the public queue page |
| `working`  | Printer controller (via API)        | G-code exported to a microSD card |
| `finished` | Printer controller (via API)        | microSD card returned after printing |
| `rejected` | Admin                               | Declined instead of approved; terminal |

## Still open / not implemented

- **Fix the Docker PrusaSlicer install step** — currently fails the build
  outright (bad/guessed release URL), so `docker compose up -d --build`
  doesn't work at all right now. This is the most pressing item; everything
  else in the Docker deployment path is blocked behind it.
- **PrusaSlicer profile**: no `.ini` config bundle exported yet, so slicing
  currently runs with bare CLI defaults (wrong nozzle/filament/print settings
  most likely). Export one from PrusaSlicer and set `PRUSA_SLICER_PROFILE`.
- **Verify PrusaSlicer actually runs on the Pi** (arch + headless) once the
  install step above is fixed — still unverified on real hardware even after
  the build itself succeeds.
- Whether `working`/`finished` API calls need any request body (e.g. an SD
  card identifier) or stay bare state transitions — not needed yet, revisit
  if the controller firmware ends up wanting to correlate cards to jobs
  server-side instead of only locally.
- No way to pull a job back out of the queue via API (only Django admin).
