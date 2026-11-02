#!/usr/bin/env python3
"""JohnCad print agent: runs on the printers' network (the school Pi).

Polls the JohnCad site over HTTPS for work, downloads sliced files in
resumable chunks, hands them to the local Bambuddy, starts staged prints when
staff click "plate cleared" on /queue/, and reports print status back. It
only makes outbound HTTPS requests to the site, so it works from a network
that accepts no inbound connections. Setup: agent/README.md.

Needs Python 3.9+ and `requests` (Raspberry Pi OS: sudo apt install
python3-requests). No Django: it only imports jobs/bambuddy_client.py from
this repo, so run it from a checkout of the repo.

Configuration is read from the environment (see johncad-agent.env.example):
  JOHNCAD_URL          https://riponhighschoolprint.com
  JOHNCAD_AGENT_KEY    jca_... from /admin/ (Printer agents) or
                       `manage.py create_agent_key`
  BAMBUDDY_URL         default http://127.0.0.1:8080
  BAMBUDDY_API_KEY     Bambuddy API key: Manage library, Add to queue, Read status
  BAMBUDDY_PRINTER_ID  Bambuddy printer id for jobs with no printer chosen
  AGENT_STATE_DIR      default /var/lib/johncad-agent
  AGENT_CHUNK_MB       download chunk size, default 4
  AGENT_POLL_INTERVAL  seconds; default: whatever the site says (15)
  BAMBUDDY_TIMEOUT     seconds, default 60
  AGENT_LOG_LEVEL      default INFO
"""
import argparse
import fcntl
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

# Import the shared Bambuddy client from this repo checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from jobs.bambuddy_client import (  # noqa: E402
    MISSING,
    QUEUE_STATUS_EVENTS,
    BambuddyClient,
    BambuddyError,
)

log = logging.getLogger('johncad-agent')

API_PATH = '/agent-api/v1/'
MAX_BACKOFF = 300
DOWNLOAD_TRIES = 5


class HomeError(Exception):
    """The JohnCad site couldn't be reached or refused a request."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class DownloadError(Exception):
    """A download didn't finish; it resumes on the next round."""


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.url = env.get('JOHNCAD_URL', '').rstrip('/')
        self.key = env.get('JOHNCAD_AGENT_KEY', '').strip()
        self.bambuddy_url = env.get('BAMBUDDY_URL', 'http://127.0.0.1:8080')
        self.bambuddy_key = env.get('BAMBUDDY_API_KEY', '').strip()
        self.default_printer_id = env.get('BAMBUDDY_PRINTER_ID', '').strip() or None
        self.state_dir = Path(env.get('AGENT_STATE_DIR', '/var/lib/johncad-agent'))
        self.chunk_size = int(float(env.get('AGENT_CHUNK_MB', '4')) * 1024 * 1024)
        interval = env.get('AGENT_POLL_INTERVAL', '').strip()
        self.poll_interval = float(interval) if interval else None
        self.bambuddy_timeout = int(env.get('BAMBUDDY_TIMEOUT', '60'))
        self.log_level = env.get('AGENT_LOG_LEVEL', 'INFO').upper()

    def problems(self):
        missing = [name for name, value in (
            ('JOHNCAD_URL', self.url),
            ('JOHNCAD_AGENT_KEY', self.key),
            ('BAMBUDDY_URL', self.bambuddy_url),
            ('BAMBUDDY_API_KEY', self.bambuddy_key),
        ) if not value]
        problems = [f'{name} is not set.' for name in missing]
        if self.url and not self.url.startswith('https://'):
            if not self.url.startswith(('http://127.0.0.1', 'http://localhost')):
                problems.append('JOHNCAD_URL must start with https:// (the key is sent with every request).')
        if self.chunk_size < 64 * 1024:
            problems.append('AGENT_CHUNK_MB is too small.')
        return problems


class Home:
    """The JohnCad site's agent API."""

    def __init__(self, config, session=None):
        self.base = config.url + API_PATH
        self.session = session or requests.Session()
        self.session.headers['Authorization'] = f'Bearer {config.key}'
        self.session.headers['User-Agent'] = 'johncad-agent/1'

    def _send(self, method, path, **kwargs):
        kwargs.setdefault('timeout', (10, 60))
        try:
            response = self.session.request(method, self.base + path, **kwargs)
        except requests.RequestException as exc:
            raise HomeError(f'Could not reach {self.base}: {exc}') from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get('error', '')
            except ValueError:
                detail = response.text[:200]
            raise HomeError(
                f'{method} {path} returned {response.status_code}: {detail}',
                response.status_code,
            )
        return response

    def get(self, path):
        return self._json(self._send('GET', path))

    def post(self, path, body=None):
        return self._json(self._send('POST', path, json=body or {}))

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except ValueError as exc:
            raise HomeError(f'Expected JSON from {response.url}, got: {response.text[:200]}') from exc

    def chunk(self, job_id, start, end):
        """Bytes start..end (inclusive) of the job's sliced file."""
        response = self._send(
            'GET', f'jobs/{job_id}/file/',
            headers={'Range': f'bytes={start}-{end}'},
            timeout=(10, 120),
        )
        expected = f'bytes {start}-{end}/'
        if response.status_code != 206 or not response.headers.get('Content-Range', '').startswith(expected):
            raise HomeError(
                f'Bad range response for job #{job_id}: HTTP {response.status_code} '
                f'{response.headers.get("Content-Range", "")!r}'
            )
        data = response.content
        if len(data) != end - start + 1:
            raise HomeError(f'Short chunk for job #{job_id}: {len(data)} bytes, expected {end - start + 1}.')
        return data


class State:
    """Reports that must reach the site even if it's unreachable right now or
    the agent restarts (e.g. "job 7 is Bambuddy queue item 31"); without
    them a job could be dispatched twice. Kept as JSON in the state dir."""

    def __init__(self, state_dir):
        self.path = state_dir / 'state.json'
        self.files = state_dir / 'files'
        self.files.mkdir(parents=True, exist_ok=True)
        self.outbox = []
        if self.path.exists():
            try:
                self.outbox = json.loads(self.path.read_text()).get('outbox', [])
            except (ValueError, OSError) as exc:
                log.error('Ignoring unreadable %s: %s', self.path, exc)

    def save(self):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'outbox': self.outbox}, indent=1))
        os.replace(tmp, self.path)

    def queue_report(self, job_id, path, body):
        self.outbox.append({'job': job_id, 'path': path, 'body': body})
        self.save()

    def busy_jobs(self):
        return {item['job'] for item in self.outbox}


class Agent:
    def __init__(self, config, home=None, bambuddy=None, state=None):
        self.config = config
        self.home = home or Home(config)
        self.bambuddy = bambuddy or BambuddyClient(
            config.bambuddy_url, config.bambuddy_key, config.bambuddy_timeout,
        )
        self.state = state or State(config.state_dir)
        self.missing_logged = set()

    # -- main loop ------------------------------------------------------------

    def run_forever(self):
        log.info('Print agent started; checking in with %s.', self.config.url)
        failures = 0
        while True:
            try:
                interval = self.run_once()
                failures = 0
            except HomeError as exc:
                failures += 1
                interval = min((self.config.poll_interval or 15) * 2 ** failures, MAX_BACKOFF)
                level = logging.ERROR if exc.status in (401, 404) else logging.WARNING
                log.log(level, '%s (retrying in %ss)', exc, int(interval))
                if exc.status == 401:
                    log.error('The site rejected JOHNCAD_AGENT_KEY. Is the agent active in /admin/?')
            time.sleep(interval)

    def run_once(self):
        """One round: send queued reports, then do what the site asks.
        Returns seconds until the next round."""
        self.flush_outbox()
        work = self.home.get('poll/')
        busy = self.state.busy_jobs()
        for item in work.get('release', []):
            if item['id'] not in busy:
                self.release(item)
        for item in work.get('dispatch', []):
            if item['id'] not in busy:
                self.dispatch(item['id'], bool(work.get('staged', True)))
        self.flush_outbox()
        for item in work.get('track', []):
            if item['id'] not in busy:
                self.track(item)
        return self.config.poll_interval or float(work.get('poll_interval') or 15)

    def flush_outbox(self):
        """Send queued reports in order. A rejected report (4xx: the job moved
        on) is dropped; an unreachable site stops the flush for now."""
        while self.state.outbox:
            item = self.state.outbox[0]
            try:
                self.home.post(item['path'], item['body'])
            except HomeError as exc:
                if exc.status is None or exc.status >= 500 or exc.status == 401:
                    raise
                log.warning('Site rejected report for job #%s, dropping it: %s', item['job'], exc)
            self.state.outbox.pop(0)
            self.state.save()

    # -- dispatch -------------------------------------------------------------

    def dispatch(self, job_id, staged):
        try:
            info = self.home.post(f'jobs/{job_id}/claim/')
        except HomeError as exc:
            if exc.status in (404, 409, 410):
                log.info('Job #%s is no longer available: %s', job_id, exc)
                return
            raise
        printer_id = info.get('printer_id') or self.config.default_printer_id
        if not printer_id:
            self.report_dispatch_failed(
                job_id, 'The job has no printer and the agent has no BAMBUDDY_PRINTER_ID.')
            return

        file_id = info.get('bambuddy_file_id') or ''
        reused = bool(file_id)
        if not reused:
            try:
                path = self.download(info)
            except (DownloadError, HomeError, OSError) as exc:
                # The claim stays ours; the next round resumes the download.
                log.warning('Job #%s download paused: %s', job_id, exc)
                return
            try:
                with open(path, 'rb') as f:
                    file_id = self.bambuddy.upload_file(f, info['file_name'])
            except (BambuddyError, OSError) as exc:
                self.report_dispatch_failed(job_id, f'Upload to Bambuddy failed: {exc}')
                return
            log.info('Job #%s uploaded to Bambuddy (file %s).', job_id, file_id)
        try:
            queue_id = self.bambuddy.enqueue(file_id, printer_id, staged)
        except BambuddyError as exc:
            # A fresh upload is kept for the retry; a reused file id that
            # failed is forgotten so the retry uploads the file again.
            self.report_dispatch_failed(
                job_id, f'Adding to the Bambuddy queue failed: {exc}',
                file_id if not reused else '',
            )
            return
        log.info('Job #%s queued in Bambuddy on printer %s (queue item %s%s).',
                 job_id, printer_id, queue_id, ', staged' if staged else '')
        self.state.queue_report(job_id, f'jobs/{job_id}/dispatched/', {
            'bambuddy_file_id': file_id, 'bambuddy_queue_id': queue_id,
        })
        self.cleanup(job_id)

    def report_dispatch_failed(self, job_id, message, file_id=None):
        log.error('Job #%s: %s', job_id, message)
        body = {'error': message}
        if file_id is not None:
            body['bambuddy_file_id'] = file_id
        self.state.queue_report(job_id, f'jobs/{job_id}/dispatch-failed/', body)
        self.cleanup(job_id)

    def _paths(self, job_id):
        base = self.state.files / f'job-{job_id}'
        return base.with_suffix('.part'), base.with_suffix('.json'), base.with_suffix('.gcode.3mf')

    def cleanup(self, job_id):
        for path in self._paths(job_id):
            path.unlink(missing_ok=True)

    def download(self, info):
        """Fetch the job's file in chunks into the state dir, resuming a
        partial download of the same file. Returns the finished file."""
        job_id, size, sha256 = info['id'], int(info['size']), info['sha256']
        part, meta, done = self._paths(job_id)
        if done.exists() and _sha256(done) == sha256:
            return done
        if size <= 0:
            raise DownloadError('The sliced file is empty on the server.')
        if part.exists():
            try:
                same = json.loads(meta.read_text()).get('sha256') == sha256
            except (ValueError, OSError):
                same = False
            if not same or part.stat().st_size > size:
                part.unlink()
        meta.write_text(json.dumps({'sha256': sha256, 'size': size}))

        offset = part.stat().st_size if part.exists() else 0
        if offset:
            log.info('Job #%s: resuming download at %s of %s bytes.', job_id, offset, size)
        with open(part, 'ab') as f:
            while offset < size:
                end = min(offset + self.config.chunk_size, size) - 1
                f.write(self._chunk_with_retries(job_id, offset, end))
                f.flush()
                offset = end + 1
        if _sha256(part) != sha256:
            part.unlink()
            raise DownloadError('Checksum mismatch; downloading again next round.')
        os.replace(part, done)
        meta.unlink(missing_ok=True)
        log.info('Job #%s downloaded (%s bytes).', job_id, size)
        return done

    def _chunk_with_retries(self, job_id, start, end):
        for attempt in range(1, DOWNLOAD_TRIES + 1):
            try:
                return self.home.chunk(job_id, start, end)
            except HomeError as exc:
                if exc.status is not None and exc.status < 500:
                    raise           # e.g. 409: our claim lapsed
                if attempt == DOWNLOAD_TRIES:
                    raise DownloadError(f'giving up after {attempt} tries: {exc}') from exc
                delay = 2 ** attempt
                log.warning('Job #%s chunk %s-%s failed (%s); retrying in %ss.',
                            job_id, start, end, exc, delay)
                time.sleep(delay)

    # -- release & status -----------------------------------------------------

    def release(self, item):
        job_id, queue_id = item['id'], item['bambuddy_queue_id']
        try:
            self.bambuddy.start_item(queue_id)
        except BambuddyError as exc:
            log.error('Job #%s: could not start queue item %s: %s', job_id, queue_id, exc)
            self.state.queue_report(job_id, f'jobs/{job_id}/release-failed/', {'error': str(exc)})
            return
        log.info('Job #%s released (queue item %s).', job_id, queue_id)
        self.state.queue_report(job_id, f'jobs/{job_id}/released/', {})

    def track(self, item):
        job_id, queue_id = item['id'], item['bambuddy_queue_id']
        try:
            status = self.bambuddy.queue_item_status(queue_id)
        except BambuddyError as exc:
            log.warning('Job #%s: could not read queue item %s: %s', job_id, queue_id, exc)
            return
        if status == MISSING:
            if job_id not in self.missing_logged:
                self.missing_logged.add(job_id)
                log.warning('Job #%s: Bambuddy queue item %s no longer exists.', job_id, queue_id)
            return
        event = QUEUE_STATUS_EVENTS.get(status)
        if event is None or (event == 'started' and item.get('status') == 'working'):
            return
        body = {'event': event}
        if event == 'failed':
            body['detail'] = f'Bambuddy queue item is "{status}".'
        try:
            self.home.post(f'jobs/{job_id}/event/', body)
            log.info('Job #%s: reported %s.', job_id, event)
        except HomeError as exc:
            if exc.status is None or exc.status >= 500:
                raise
            log.warning('Job #%s: site rejected %s: %s', job_id, event, exc)


def acquire_lock(state_dir):
    """Only one agent may use a state dir: two would write the same partial
    downloads and could dispatch a job twice. Returns the open lock file
    (keep it open), or None if another agent holds it."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = open(state_dir / 'agent.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return None
    return lock


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--once', action='store_true', help='Run one round and exit.')
    parser.add_argument('--check', action='store_true',
                        help='Check the configuration and the connection to the site, then exit.')
    args = parser.parse_args(argv)

    config = Config()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format='%(levelname)s %(message)s',
    )
    problems = config.problems()
    if problems:
        for problem in problems:
            log.error(problem)
        return 2

    if args.check:
        try:
            work = Home(config).get('poll/')
        except HomeError as exc:
            log.error('%s', exc)
            return 1
        print(f'Connected to {config.url} as agent "{work.get("agent")}".')
        print(f'Waiting to dispatch: {len(work.get("dispatch", []))}, to release: '
              f'{len(work.get("release", []))}, in flight: {len(work.get("track", []))}.')
        return 0

    lock = acquire_lock(config.state_dir)
    if lock is None:
        log.error('Another agent is already running with %s (is the johncad-agent '
                  'service running? Stop it first to run one by hand).', config.state_dir)
        return 1
    agent = Agent(config)
    if args.once:
        try:
            agent.run_once()
        except HomeError as exc:
            log.error('%s', exc)
            return 1
        return 0
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
