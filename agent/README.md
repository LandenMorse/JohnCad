# Print agent: site at home, printers at school

Use this when JohnCad is hosted somewhere other than the printers' network
(e.g. a Pi at home) and that network allows only outbound connections. A small
agent on a Pi at school checks in with the site over normal HTTPS (port 443),
downloads approved, sliced jobs, and hands them to Bambuddy on the same Pi.
Nothing at school accepts inbound connections, and there's no tunnel.

```
students/teachers ──HTTPS──> home Pi 5: Caddy -> gunicorn (JohnCad) + slice_worker (OrcaSlicer)
                                   ^
                                   │ HTTPS 443, outbound only, every 15 s
                                   │
school Pi: johncad-agent ──> Bambuddy (127.0.0.1:8080) ──> printers (LAN)
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

Slicing stays on the site's Pi (`slice_worker` + OrcaSlicer). The school Pi
only moves finished files.

## On the site (home Pi 5)

1. Pull this branch, then:
   ```bash
   .venv/bin/pip install -r requirements.txt
   .venv/bin/python manage.py migrate
   .venv/bin/python manage.py collectstatic --noinput
   ```
2. Add to `.env`:
   ```
   BAMBUDDY_VIA_AGENT=True
   ```
   `BAMBUDDY_URL`, `BAMBUDDY_API_KEY`, `BAMBUDDY_PRINTER_ID` and
   `BAMBUDDY_WEBHOOK_SECRET` aren't used here any more. They go in the
   agent's config instead (keep `BAMBUDDY_STAGED`, which the agent follows).
3. Bambuddy no longer runs on this Pi, so turn off the dispatcher and keep the
   slicer:
   ```bash
   sudo systemctl disable --now johncad-dispatcher
   sudo systemctl restart johncad johncad-slicer
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
5. Printers stay in `/admin/` > **Printers** as before. Their **Bambuddy
   printer id** is the id in the *school* Pi's Bambuddy.

Caddy needs no change. The agent API is under `/agent-api/`, not `/api/`, so
`respond /api/* 404` doesn't block it. It returns 404 unless
`BAMBUDDY_VIA_AGENT=True`, and 401 without a valid key.

## On the school Pi

**First check that the school network lets the Pi reach the site.** Run this
on the school WiFi:
```bash
curl -sI https://riponhighschoolprint.com/login/ | head -1
```
You want `HTTP/2 200` or `HTTP/1.1 200`. If it hangs or fails, the domain needs
to be allowed for the Pi first (ask IT; the agent can't work around it).

1. **Get the code** (the agent imports `jobs/bambuddy_client.py` from it; no
   Django or Postgres needed here):
   ```bash
   git clone https://github.com/LandenMorse/johncad.git ~/JohnCad
   cd ~/JohnCad && git checkout agent-api
   sudo apt install -y python3-requests
   ```
2. **Bambuddy** (needs Docker): `docker compose up -d bambuddy` from the
   checkout starts only Bambuddy. Then follow the main README's "Bambuddy
   setup" steps 1–2: add the printers, hand-print one file, and create an API
   key with **Manage library**, **Add to queue** and **Read status**. Skip
   the webhook step; the agent reads print status from Bambuddy's queue
   instead.
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
   It should print `Connected to https://riponhighschoolprint.com as agent "school-pi".`
5. **Run it as a service** (replace `<user>` in the file first):
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

`git pull` on both Pis. On the site, also run `migrate` + `collectstatic` and
restart `johncad johncad-slicer`. On the school Pi, `sudo systemctl restart
johncad-agent`.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `--check`: `returned 401` | Wrong key, or the agent is inactive in `/admin/` |
| `--check`: `returned 404` | `BAMBUDDY_VIA_AGENT=True` missing on the site, or gunicorn not restarted |
| `--check`: `Could not reach` / timeout | School network blocks the domain for the Pi, or the site is down |
| Job stuck at "Sending to printer…" | Agent offline (see `/queue/`), or still downloading: `journalctl -u johncad-agent` |
| "Sending failed: … BAMBUDDY_PRINTER_ID" | Job has no printer and the agent has no fallback id |
| "Sending failed: Adding to the Bambuddy queue failed …" | Wrong Bambuddy printer id on the Printer in `/admin/`, or printer offline in Bambuddy. Fix it, then **Retry** on `/review/` (the upload is reused) |
| "Last release failed: … 409" on `/queue/` | Bambuddy thinks the loaded spool is too short. Check the filament, then release again |
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

An agent only sees jobs for the printers ticked on it in `/admin/` (none =
all printers), plus jobs with no printer if **Handles unassigned** is on.
