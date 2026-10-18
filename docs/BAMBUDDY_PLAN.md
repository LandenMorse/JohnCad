# JohnCad + Bambuddy plan

Replace the Arduino/microSD step with automatic dispatch to Bambu printers
through a self-hosted [Bambuddy](https://github.com/maziggy/bambuddy) on the
home Pi. Everything upstream stays: signup, classes, teacher review,
`slice_worker`, `/queue/`.

## Architecture

```
Student upload → teacher approves (/review/) → slice_worker (OrcaSlicer, .gcode.3mf)
→ dispatch_worker → Bambuddy REST API (home Pi)
→ Bambuddy → printers (FTPS 990 + MQTT 8883)
→ Bambuddy webhook → Django → job status updates
```

Django never talks to printers, only to Bambuddy.

## Network (school site)

- Printers sit behind a **GL.iNet travel router** that joins the school guest
  Wi-Fi as a client and broadcasts a private 2.4 GHz SSID on its own subnet
  (e.g. `192.168.77.0/24`). This avoids the captive portal and client
  isolation for the printers.
- The router runs **Tailscale as a subnet router** advertising that subnet.
  The home Pi runs Tailscale with `--accept-routes`.
- Disable Tailscale key expiry for the router and the Pi. Put printers on
  DHCP reservations. Use a shared/org Tailscale account.
- Get school IT approval. Captive portal re-logins are the biggest risk to
  uptime.
- Printers need **LAN Mode** enabled. Turn off printer auto-updates, since
  recent firmware can restrict LAN commands.

## Code changes in JohnCad

Status: implemented on branch `claude/bambuddy-dispatch-hx3ux3` unless noted.

1. **Slicer output** — `jobs/slicing.py` `slice_to_3mf()` passes
   `--export-3mf` and stores the result in `Job.sliced_file` (renamed from
   `gcode`). Unverified until a file prints (build order step 1).
2. **Job model** — `bambuddy_file_id`, `bambuddy_queue_id`,
   `dispatch_started_at`, `dispatched_at`, `dispatch_error`, `released_at`,
   `failed_at`, and a `failed` status (migration `0004`).
3. **`dispatch_worker` command** — modeled on `slice_worker`: claims `queued`
   jobs with no `bambuddy_queue_id`, uploads, enqueues, stores the ids; errors
   are saved on the job for the teacher **Retry** button on `/review/`.
   Logic in `jobs/dispatch.py`. Runs as the `johncad-dispatcher` systemd unit
   (README "Dispatch worker").
4. **Webhook** — `POST /api/bambuddy/webhook/` (`jobs/api_views.py`):
   shared-secret check, idempotent, `print_started` → `working`,
   `print_complete` → `finished`, `print_failed`/`print_stopped` (or a
   non-success complete) → `failed`. Invalid transitions return 409. The
   dispatch worker polls Bambuddy's queue every 60s as a fallback.
5. **Env vars** — `BAMBUDDY_URL`, `BAMBUDDY_API_KEY`,
   `BAMBUDDY_WEBHOOK_SECRET`, plus `BAMBUDDY_PRINTER_ID`, `BAMBUDDY_STAGED`,
   `BAMBUDDY_TIMEOUT` (`.env.example`).
6. **Deploy** — `bambuddy` service in `docker-compose.yml` (host networking,
   port 8080). The webhook goes straight to gunicorn on `127.0.0.1:8000`;
   both nginx configs return 404 for `/api/`.
7. **Bed-clear** — jobs are enqueued staged ("Queue Only"); staff click
   **Plate cleared, release next** on `/queue/`.
8. **Removed** — `firmware/`, the `/api/jobs/...` endpoints, DRF and its
   token auth, `PRINTER_CONTROLLER_API.md`.

## Bambuddy facts

From the Bambuddy wiki (wiki.bambuddy.cool) and repo, not yet from running it:

- License: **AGPL-3.0** (repo `LICENSE`).
- API base `/api/v1`; key sent as `X-API-Key`. Key permissions are toggles
  (Read Status, Manage Queue, Control Printer, Manage Library, …), not a
  `can_queue` scope. A built-in interactive API browser is available.
- `POST /queue` documented body: `{"archive_id", "printer_id", "scheduled_time"}`.
- `POST /webhook/printer/{id}/start` starts the printer's next pending item,
  releasing one waiting for manual start (needs Control Printer).
- `GET /queue?printer_id=&status=` lists queue items (no per-item GET).
- Outgoing webhooks (Settings > Notifications): `{"event": "print_complete",
  "timestamp", "data": {"printer_id", "printer_name", "archive_id",
  "filename", "duration", "status"}}`; events `print_started`,
  `print_progress`, `print_complete`, `print_failed`, `print_stopped`,
  `printer_offline`, `printer_error`. No signature and no queue item id, so
  JohnCad names uploads `johncad-job-<id>.gcode.3mf` and matches webhooks on
  that filename, then on `archive_id`.
- Docker image `ghcr.io/maziggy/bambuddy`, port 8000 by default
  (`PORT` env), host networking on Linux.

## Unverified, check first

Every guess lives in `jobs/bambuddy.py` as a `TODO(verify)` constant. Copy
the real values from Bambuddy's API browser (`<BAMBUDDY_URL>/docs`):

- File upload endpoint (`/library/files/upload`?), multipart field name, and
  which response field is the id.
- Whether an uploaded library file's id is what `POST /queue` wants as
  `archive_id`, the queue response's id field, and how to request "Queue
  Only" / manual start (`manual_start` is a guess).
- Queue item status values used by the polling fallback.
- Whether the webhook can send a custom header; if not, put the secret in the
  URL (`?secret=`).
- Whether Orca's CLI 3MF output prints unchanged on your printer model.

## Build order

1. Hand-upload one `.gcode.3mf` through Bambuddy and print it.
2. Script one upload and enqueue to one hardcoded printer.
3. Add `dispatch_worker` and the model fields. *(done in code)*
4. Add the webhook and status sync. *(done in code)*
5. Add the bed-clear button *(done in code)*, then multi-printer and model
   routing.

## Repo cleanup before open-sourcing

Add a LICENSE, remove dead Arduino code *(done)* and unused compose services,
pick one deployment path, add tests for the job lifecycle and slicing
*(started)*, make domain settings configurable, and add a short quick-start
to the README.

## Long-term maintenance

Pin the Bambuddy version, write a runbook (first checks when printers show
offline), back up databases, and train one successor.
