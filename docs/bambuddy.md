# JohnCad and Bambuddy

JohnCad hands approved, sliced jobs to Bambu printers through a self-hosted
[Bambuddy](https://github.com/maziggy/bambuddy). Django never talks to the
printers, only (indirectly) to Bambuddy's REST API.

## How it runs at the school

```
Student upload -> teacher approves (/review/) -> slice_worker (OrcaSlicer, .gcode.3mf)
                                    site Pi (Pi 5, at home)
                                          ^
                                          | HTTPS 443, outbound only, every 15 s
                                          |
printer Pi (Pi 3, at school): johncad-agent -> Bambuddy REST API -> Bambuddy -> printers
                                                          (FTPS 990 + MQTT 8883 on the school LAN)
```

- The **site Pi** has `BAMBUDDY_VIA_AGENT=True`. It slices jobs and offers
  them to agents at `/agent-api/v1/`; it never contacts Bambuddy.
- The **print agent** on the printer Pi (`agent/johncad_agent.py`) polls the
  site, downloads each sliced file, uploads it to the local Bambuddy and adds
  it to the printer's queue as a staged item ("Queue Only"). Staff click
  **Plate cleared, release next** on `/queue/`; the agent starts the print and
  reports its status back by reading Bambuddy's queue.
- **Adding printers** goes the same way: a teacher files a request on
  `/printers/`, the agent adds the printer to Bambuddy, waits until Bambuddy
  is connected to it, and reports its Bambuddy id; the site then creates the
  printer. Not available in direct mode.
- Setup: [agent/README.md](../agent/README.md). Day-to-day:
  [operations.md](operations.md#the-printer-pi).

### Network

- The printer Pi needs **outbound HTTPS (port 443) to the site's domain**.
  That is the only thing to ask school IT for: nothing at school accepts
  inbound connections, and no tunnel or VPN is involved.
- The printer Pi and the printers must be on the same local network.
- Printers need **LAN Mode** enabled. Turn off printer auto-updates: recent
  firmware can restrict LAN commands.

## Direct mode (optional, not used at the school)

If the site and the printers are on the same network, Bambuddy can run on the
site Pi itself and the site talks to it directly, with no agent
(`BAMBUDDY_VIA_AGENT=False`):

- `dispatch_worker` (`jobs/dispatch.py`, the `johncad-dispatcher` systemd
  unit, which `setup-site.sh` does not install; the unit file is
  `deploy/systemd/johncad-dispatcher.service`) uploads and enqueues jobs, and
  polls each queue item every 60 s.
- Bambuddy reports print events to `POST /api/bambuddy/webhook/`
  (`BAMBUDDY_WEBHOOK_SECRET`; webhook payload format **generic**, **Auth
  header** = the secret). Caddy returns 404 for `/api/`, so only Bambuddy on
  the same machine reaches it.
- The `BAMBUDDY_*` settings in `.env.example` configure it.

## Code map

| | |
|---|---|
| `jobs/slicing.py` | `slice_to_3mf()`: OrcaSlicer CLI with `--export-3mf`, result in `Job.sliced_file` |
| `jobs/bambuddy_client.py` | Every Bambuddy API detail as constants, plus the client; no Django imports, shared with the agent |
| `jobs/agent_api.py`, `agent_urls.py` | The `/agent-api/v1/` endpoints |
| `jobs/dispatch.py` | Job state changes: direct dispatch, status sync, release-next, and the agent's claim/report calls |
| `agent/johncad_agent.py` | The print agent |
| `jobs/printer_tickets.py` | Add-printer ticket state changes (claim, complete, fail, cancel) |
| `jobs/bambuddy.py`, `api_views.py` | Direct mode only: Django wrapper and webhook |

## Bambuddy API facts

From Bambuddy's source (github.com/maziggy/bambuddy, commit `98ecaf5`,
2026-10-22; changelog's latest release then 1.2.5.7) and in use against a
live Bambuddy through the agent:

- License: **AGPL-3.0**. JohnCad only calls its HTTP API, so it isn't
  affected.
- API base `/api/v1`. Key sent as `X-API-Key` (or `Authorization: Bearer
  bb_...`). The key needs **Manage library** (upload), **Add to queue**
  (enqueue and start) and **Read status** (polling), plus permission to add
  and remove printers. Not Control Printer.
- Printers (from the [API reference](https://wiki.bambuddy.cool/reference/api/),
  read 2026-11-13; **not yet run against a live Bambuddy**): add with
  `POST /printers` and `{"name", "ip_address", "access_code",
  "serial_number"}`; `GET /printers` lists them with `id` and
  `serial_number`; `GET /printers/{id}/status` has `"connected": true` once
  Bambuddy has reached the printer; `DELETE /printers/{id}?delete_archives=false`
  removes one. The agent adds a printer, finds its id by serial number, waits
  for `connected`, and removes it again if it never connects.
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
- Outgoing webhook (direct mode; Settings > Notifications, provider
  **Webhook**, payload format **generic**): a flat JSON body `{"title",
  "message", "timestamp", "source": "Bambuddy", "event", "printer",
  "filename", ...}`. Events used: `print_start`, `print_complete`,
  `print_failed`, `print_stopped` (each has to be switched on for the
  provider). `filename` is the print's subtask name: the library filename
  with `.gcode`/`.3mf` stripped, so a JohnCad upload shows up as
  `johncad-job-<id>`. No printer, archive or queue id is sent, so the job id
  in that name is what links the event to a job. The provider's **Auth
  header** value is sent as `Authorization: Bearer <value>`.
- Docker image `ghcr.io/maziggy/bambuddy`, port 8000 by default (`PORT`
  env; the compose file sets 8080), host networking on Linux, data in
  `/app/data` and `/app/logs`.

## Open items

- "Any X1C"-style automatic printer choice: teachers pick the printer by
  hand on `/review/` for now.
- Pin the Bambuddy image version (`BAMBUDDY_VERSION`, currently `latest`).
- Direct mode (dispatcher and webhook) is kept as an alternative but is not
  used or tested against a live Bambuddy; remove it if it stays unused.
