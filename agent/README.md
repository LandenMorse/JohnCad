# Print agent: the printer Pi

This is how JohnCad reaches the printers. The site runs on one Pi (the
**site Pi**, a Pi 5 at home); a second Pi on the printers' network (the
**printer Pi**, a Pi 3 at school) runs Bambuddy and this agent. The agent
checks in with the site over normal HTTPS (port 443), downloads approved,
sliced jobs, and hands them to Bambuddy on the same Pi. Nothing at school
accepts inbound connections, and there is no tunnel or VPN.

```
students/teachers ──HTTPS──> site Pi: Caddy -> gunicorn (JohnCad) + slice_worker (OrcaSlicer)
                                   ^
                                   │ HTTPS 443, outbound only, every 15 s
                                   │
printer Pi: johncad-agent ──> Bambuddy (127.0.0.1:8080) ──> printers (LAN)
```

What the agent does each round:

1. Sends any reports it couldn't deliver earlier (they're saved to disk, so a
   restart or an outage never makes it forget that a job was already queued).
2. Asks the site for work (`GET /agent-api/v1/poll/`).
3. Starts staged prints that staff released on `/queue/` ("Plate cleared,
   release next").
4. Claims each waiting job, downloads its `.gcode.3mf` in 4 MB chunks
   (resuming where it stopped if the connection drops), checks the SHA-256,
   uploads it to Bambuddy and adds it to the printer's queue (staged).
5. Reads each in-flight job's Bambuddy queue item and reports `started`,
   `completed` or `failed`, which moves the job along on the site.

Slicing stays on the site Pi (`slice_worker` + OrcaSlicer). The printer Pi
only moves finished files.

## On the site Pi

1. Install or update the site (`scripts/setup-site.sh` the first time,
   `scripts/update.sh` afterwards; see the main README).
2. `.env` must contain:
   ```
   BAMBUDDY_VIA_AGENT=True
   ```
   (`setup-site.sh` writes it.) `BAMBUDDY_URL`, `BAMBUDDY_API_KEY`,
   `BAMBUDDY_PRINTER_ID` and `BAMBUDDY_WEBHOOK_SECRET` aren't used on the site
   in this setup. They go in the agent's config instead (keep
   `BAMBUDDY_STAGED`, which the agent follows).
3. Only `johncad` and `johncad-slicer` run here. If this Pi still has the
   old `johncad-dispatcher` service from an earlier setup, turn it off:
   ```bash
   sudo systemctl disable --now johncad-dispatcher
   ```
4. Create the agent's key, either in `/admin/` > **Printer agents** > Add (the
   key is shown once at the top of the page after saving), or:
   ```bash
   .venv/bin/python manage.py create_agent_key school-pi
   ```
   Copy the `jca_...` key; only a hash of it is stored. To replace a lost or
   leaked key: the admin action **Generate a new key**, or
   `create_agent_key school-pi --rotate`. Unticking **Is active** blocks the
   agent immediately.
5. In `/admin/`, add a **Printer model** for each kind of printer (a name
   and the OrcaSlicer machine + process preset `.json` paths, `;`-separated)
   and a **Filament** for each filament you stock (type, color, filament
   preset `.json` path).
6. Once the agent is running (next section), teachers add printers on the
   site's **Printers** page (`/printers/`): name, model, IP address, access
   code and serial number. The agent adds the printer to its Bambuddy, waits
   until Bambuddy is connected to it, and reports back; the page shows
   "Added", or the reason it failed. Teachers then choose the printer when
   approving a job on `/review/`; it decides the slicer presets and where the
   job is sent. Until a printer exists, jobs use `ORCA_SLICER_SETTINGS` from
   `.env` and the agent's `BAMBUDDY_PRINTER_ID` (single-printer setup).

Caddy needs no change. The agent API is under `/agent-api/`, not `/api/`, so
`respond /api/* 404` doesn't block it. It returns 404 unless
`BAMBUDDY_VIA_AGENT=True`, and 401 without a valid key.

## On the printer Pi

Any Pi on the printers' network that can run Docker works (the school's is a
Pi 3).

**First check that the school network lets the Pi reach the site.** Run this
on the school network:
```bash
curl -sI https://<your-domain>/login/ | head -1
```
You want `HTTP/2 200` or `HTTP/1.1 200`. If it hangs or fails, the domain needs
to be allowed for the Pi first (ask IT; the agent can't work around it).

1. **Get the code** (the agent imports `jobs/bambuddy_client.py` from it; no
   Django or Postgres needed here):
   ```bash
   git clone https://github.com/LandenMorse/JohnCad.git ~/JohnCad
   sudo apt install -y python3-requests
   ```
2. **Bambuddy** (needs Docker): `docker compose up -d bambuddy` from the
   checkout starts only Bambuddy (`docker-compose.yml` also has the site's
   database, which you don't start here). Open `http://<printer pi>:8080` and
   create an API key (Settings > API keys) with **Manage library**, **Add to
   queue**, **Read status**, and permission to **add and remove printers**
   (the agent adds the printers teachers request on the site's Printers page,
   and removes one again if Bambuddy can't connect to it). Printers need LAN
   Mode on and auto-updates off. There is no webhook to set up; the agent
   reads print status from Bambuddy's queue. Pin `BAMBUDDY_VERSION` in a `.env` next to
   `docker-compose.yml` once you have a version that works.
3. **Config**:
   ```bash
   sudo cp agent/johncad-agent.env.example /etc/johncad-agent.env
   sudo nano /etc/johncad-agent.env      # JOHNCAD_AGENT_KEY, BAMBUDDY_API_KEY, BAMBUDDY_PRINTER_ID
   sudo chown $USER /etc/johncad-agent.env && sudo chmod 600 /etc/johncad-agent.env
   ```
4. **Test the connection**:
   ```bash
   set -a; . /etc/johncad-agent.env; set +a
   python3 agent/johncad_agent.py --check
   ```
   It should print `Connected to https://<your-domain> as agent "school-pi".`
5. **Run it as a service** (replace `<user>` in the file first; the path in
   it is where you cloned the repo):
   ```bash
   sudo cp agent/johncad-agent.service /etc/systemd/system/
   sudo nano /etc/systemd/system/johncad-agent.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now johncad-agent
   journalctl -u johncad-agent -f
   ```
   On `/queue/`, staff see "Print agent school-pi: last checked in … ago".

To run one round by hand (`python3 agent/johncad_agent.py --once`), stop the
service first. Only one agent may use a state folder at a time, and the
second one refuses to start.

## Updating

Update the site Pi first with `scripts/update.sh`. Then on the printer Pi:

```bash
cd ~/JohnCad && git pull
sudo systemctl restart johncad-agent
```

The agent imports `jobs/bambuddy_client.py` from this checkout, so keep the
two Pis on the same version.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `--check`: `returned 401` | Wrong key, or the agent is inactive in `/admin/` |
| `--check`: `returned 404` | `BAMBUDDY_VIA_AGENT=True` missing from the site Pi's `.env`, or gunicorn not restarted |
| `--check`: `Could not reach` / timeout | School network blocks the domain for the Pi, or the site Pi is down |
| Job stuck at "Sending to printer…" | Agent offline (see `/queue/`), or still downloading: `journalctl -u johncad-agent` |
| "Sending failed: … BAMBUDDY_PRINTER_ID" | Job has no printer and the agent has no fallback id |
| "Sending failed: Adding to the Bambuddy queue failed …" | Wrong Bambuddy printer id on the Printer in `/admin/`, or printer offline in Bambuddy. Fix it, then **Retry** on `/review/` (the upload is reused) |
| "Last release failed: … 409" on `/queue/` | Bambuddy thinks the loaded spool is too short. Check the filament, then release again |
| Printer request stays "Waiting for the print agent" | Agent offline, or running an older version: `git pull` and restart it |
| Printer request failed: "could not connect to the printer" | Printer off, LAN Mode off, or a wrong IP address, access code or serial number. Fix it and add the printer again |
| Printer request failed: "returned 403" | The Bambuddy API key lacks permission to add printers |
| Download keeps restarting | Checksum mismatch: the file changed on the site. It re-downloads on its own |

## API (for reference)

All under `/agent-api/v1/`, `Authorization: Bearer <key>`, JSON bodies.

| Method & path | Purpose |
|---|---|
| `GET poll/` | `dispatch`, `release`, `track` lists + `poll_interval`, `staged` |
| `POST jobs/<id>/claim/` | Claim a job; returns `size`, `sha256`, `file_name`, `printer_id`, `bambuddy_file_id` |
| `GET jobs/<id>/file/` | The sliced file; one `Range: bytes=a-b` per request. Each request keeps the claim alive (it lapses after `AGENT_CLAIM_TIMEOUT`, default 15 min, without one) |
| `POST jobs/<id>/dispatched/` | `{bambuddy_file_id, bambuddy_queue_id}`; repeats are no-ops |
| `POST jobs/<id>/dispatch-failed/` | `{error, bambuddy_file_id?}`; shown on `/review/` with Retry |
| `POST jobs/<id>/released/` | Staged print started |
| `POST jobs/<id>/release-failed/` | `{error}`; shown on `/queue/`, staff can release again |
| `POST jobs/<id>/event/` | `{event: started/completed/failed, detail?}` |
| `POST printer-tickets/<id>/claim/` | The agent is about to add this printer (from `poll/`'s `printer_tickets` list: `id`, `name`, `ip_address`, `access_code`, `serial_number`). Repeatable by the same agent |
| `POST printer-tickets/<id>/done/` | `{bambuddy_printer_id}`: added and connected; the site creates the printer. Repeats are no-ops |
| `POST printer-tickets/<id>/failed/` | `{error}`; shown on `/printers/` |

An agent only sees jobs for the printers ticked on it in `/admin/` (none =
all printers), plus jobs with no printer if **Handles unassigned** is on.
