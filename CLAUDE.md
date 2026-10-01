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
  the OrcaSlicer CLI (`subprocess.run`, no `shell=True`) to convert the
  attachment's `.stl` into `job.gcode`. OrcaSlicer is a **Flatpak on the Pi**
  (`ORCA_SLICER_CMD`, default `flatpak run com.orcaslicer.OrcaSlicer`) and must
  run on the host, not in the `web` container. Config via settings/env:
  `ORCA_SLICER_SETTINGS` (machine+process preset JSONs, `;`-separated,
  required), `ORCA_SLICER_FILAMENTS`, `ORCA_SLICER_DATADIR`,
  `ORCA_SLICER_TIMEOUT` (300s). Scratch output goes in
  `MEDIA_ROOT/slicing_tmp` (not `/tmp`) because the Flatpak sandbox has a
  private `/tmp`; the sandbox needs `flatpak override --filesystem=` for
  `MEDIA_ROOT` and the preset dir. Raises `SlicerNotConfigured` if settings are
  blank or the command isn't found, `SlicingFailed` (with captured
  stderr) on non-zero exit/timeout/no `.gcode` output — both caught and
  surfaced by the admin approve action. **Unverified on the Pi** (flags taken
  from `--help`, not yet run end-to-end).
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

- **Verify OrcaSlicer end-to-end on the Pi**: confirm the CLI invocation
  works headless via Flatpak, the Flatpak filesystem overrides are in place,
  and the preset paths in `ORCA_SLICER_SETTINGS` / `ORCA_SLICER_FILAMENTS` are
  right (output is Bambu-flavored G-code; confirm it suits the microSD flow).
- **Docker deployment**: `web` container can't slice (no Flatpak access); for
  production run Django on the host with Postgres (and optionally nginx) in
  Docker, and adjust `docker-compose.yml`/nginx upstream accordingly.
- Whether `working`/`finished` API calls need any request body (e.g. an SD
  card identifier) or stay bare state transitions — not needed yet, revisit
  if the controller firmware ends up wanting to correlate cards to jobs
  server-side instead of only locally.
- No way to pull a job back out of the queue via API (only Django admin).
