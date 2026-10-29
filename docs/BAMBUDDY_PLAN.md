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

Status: implemented on branch `claude/bambuddy-integration-ok9pdr` unless noted.

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
   shared-secret check, idempotent, `print_start` → `working`,
   `print_complete` → `finished`, `print_failed`/`print_stopped` → `failed`.
   Invalid transitions return 409. The dispatch worker polls each job's
   Bambuddy queue item every 60s as a fallback.
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

## Bambuddy API facts

Checked against Bambuddy's source (github.com/maziggy/bambuddy, commit
`98ecaf5`, 2026-10-22; changelog's latest release 1.2.5.7), not yet against a
running instance:

- License: **AGPL-3.0**. JohnCad only calls its HTTP API, so it isn't
  affected.
- API base `/api/v1`. Key sent as `X-API-Key` (or `Authorization: Bearer
  bb_...`). JohnCad's key needs **Manage library** (upload), **Add to queue**
  (enqueue and start) and **Read status** (polling). Not Control Printer.
- Upload: `POST /library/files/`, multipart field `file`, response
  `{"id", "filename", "file_type", "file_size", "duplicate_of", ...}`.
- Enqueue: `POST /queue/` with `{"library_file_id", "printer_id",
  "manual_start"}` (the body also takes `archive_id` instead of
  `library_file_id`, `target_model`, `scheduled_time`, `plate_id`,
  `ams_mapping`, ...). Response is the queue item with `id`.
- Release: `POST /queue/{id}/start` clears `manual_start` on one item. 400 if
  it's no longer pending, 409 if Bambuddy thinks the spool is short (retry
  with `?skip_filament_check=true`; JohnCad just shows the error).
- `GET /queue/{id}` returns one item; `status` is one of `pending`,
  `printing`, `completed`, `failed`, `skipped`, `cancelled`.
- Outgoing webhook (Settings > Notifications, provider **Webhook**, payload
  format **generic**): a flat JSON body `{"title", "message", "timestamp",
  "source": "Bambuddy", "event", "printer", "filename", ...}`. Events used:
  `print_start`, `print_complete`, `print_failed`, `print_stopped` (each has
  to be switched on for the provider). `filename` is the print's subtask
  name: the library filename with `.gcode`/`.3mf` stripped, so a JohnCad
  upload shows up as `johncad-job-<id>`. No printer, archive or queue id is
  sent, so the job id in that name is what links the event to a job.
  The provider's **Auth header** value is sent as `Authorization: Bearer
  <value>`. Loopback URLs are allowed.
- Docker image `ghcr.io/maziggy/bambuddy`, port 8000 by default (`PORT`
  env), host networking on Linux, data in `/app/data` and `/app/logs`.

## Still unverified

- A live run: upload, enqueue, start, and the webhook arriving with the
  expected `filename`.
- Whether OrcaSlicer's CLI `.gcode.3mf` output prints unchanged on the
  printer model.
- Which image tag to pin (`BAMBUDDY_VERSION`, currently `latest`).

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
