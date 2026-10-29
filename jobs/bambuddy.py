"""Thin client for the self-hosted Bambuddy REST API.

Django never talks to printers; everything goes through Bambuddy, which runs
on the same host. Every Bambuddy-specific detail (paths, field names, header
names, status values) is kept in this module so it can be corrected in one
place.

Checked against Bambuddy's source (github.com/maziggy/bambuddy, commit
98ecaf5, 2026-10-22): backend/app/api/routes/library.py and print_queue.py,
schemas/library.py and print_queue.py, core/auth.py, and
services/notification_service.py. Not yet checked against a running
instance; Bambuddy's API browser is at <BAMBUDDY_URL>/docs.
"""
import hmac
import re

import requests
from django.conf import settings

API_PREFIX = '/api/v1'
# core/auth.py accepts X-API-Key or "Authorization: Bearer bb_...". The key
# needs Bambuddy's "Manage Library" (upload), "Add to queue" (enqueue and
# start a staged item) and "Read status" (polling) permissions.
API_KEY_HEADER = 'X-API-Key'

# POST /library/files/ takes a multipart "file" and returns
# FileUploadResponse {"id", "filename", ...}.
UPLOAD_PATH = '/library/files/'
UPLOAD_FIELD = 'file'

# POST /queue/ takes PrintQueueItemCreate: either archive_id or
# library_file_id, plus printer_id and manual_start ("staged": the item waits
# for POST /queue/{id}/start). Returns PrintQueueItemResponse {"id", ...}.
QUEUE_PATH = '/queue/'
QUEUE_ITEM_PATH = '/queue/{item_id}'
START_PATH = '/queue/{item_id}/start'

# PrintQueueItem.status: pending, printing, completed, failed, skipped,
# cancelled. Used by the polling fallback; "pending" maps to no event.
QUEUE_STATUS_EVENTS = {
    'printing': 'started',
    'completed': 'completed',
    'failed': 'failed',
    'skipped': 'failed',
    'cancelled': 'failed',
}

# Outgoing webhook (Settings > Notifications, provider "Webhook", payload
# format "generic"). The payload is flat:
#   {"title", "message", "timestamp", "source": "Bambuddy",
#    "event": "print_complete", "printer", "filename", "reason", ...}
# There is no printer id, archive id or queue item id. "filename" is the
# print's subtask name, which Bambuddy derives from the library filename with
# ".gcode"/".3mf" stripped, so jobs are matched on the job id carried in the
# uploaded filename (see upload_name).
WEBHOOK_EVENTS = {
    'print_start': 'started',
    'print_complete': 'completed',
    'print_failed': 'failed',
    'print_stopped': 'failed',
}
# Bambuddy's webhook "Auth header" setting is sent as
# "Authorization: Bearer <value>"; ?secret= in the URL also works.
WEBHOOK_SECRET_PARAM = 'secret'


UPLOAD_NAME = 'johncad-job-{job_id}.gcode.3mf'
_JOB_NAME_RE = re.compile(r'\bjohncad-job-(\d+)\b')


def upload_name(job_id):
    return UPLOAD_NAME.format(job_id=job_id)


class BambuddyNotConfigured(Exception):
    pass


class BambuddyError(Exception):
    pass


def _url(path):
    if not settings.BAMBUDDY_URL or not settings.BAMBUDDY_API_KEY:
        raise BambuddyNotConfigured('BAMBUDDY_URL and BAMBUDDY_API_KEY must be set.')
    return settings.BAMBUDDY_URL.rstrip('/') + API_PREFIX + path


def _printer_id():
    if not settings.BAMBUDDY_PRINTER_ID:
        raise BambuddyNotConfigured('BAMBUDDY_PRINTER_ID is not set.')
    return _as_int(settings.BAMBUDDY_PRINTER_ID)


def _request(method, path, **kwargs):
    url = _url(path)
    try:
        response = requests.request(
            method,
            url,
            headers={API_KEY_HEADER: settings.BAMBUDDY_API_KEY},
            timeout=settings.BAMBUDDY_TIMEOUT,
            **kwargs,
        )
    except requests.RequestException as exc:
        raise BambuddyError(f'Could not reach Bambuddy at {settings.BAMBUDDY_URL}: {exc}') from exc
    if response.status_code >= 400:
        raise BambuddyError(
            f'Bambuddy {method} {path} returned {response.status_code}: {response.text[:500]}'
        )
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise BambuddyError(f'Bambuddy {method} {path} returned non-JSON: {response.text[:200]}') from exc


def _id(data, what):
    if isinstance(data, dict) and data.get('id') not in (None, ''):
        return str(data['id'])
    raise BambuddyError(f'Bambuddy {what} response has no id: {data!r}'[:500])


def upload_file(fileobj, filename):
    """Upload a sliced .gcode.3mf to Bambuddy's library. Returns its file id."""
    data = _request('POST', UPLOAD_PATH, files={UPLOAD_FIELD: (filename, fileobj)})
    return _id(data, 'upload')


def enqueue(file_id, printer_id=None):
    """Add an uploaded library file to a printer's queue. Returns the queue
    item id. `printer_id` is Bambuddy's id for the job's printer; without one
    the BAMBUDDY_PRINTER_ID setting is used. With BAMBUDDY_STAGED the item
    waits for start_item() (bed clear).
    """
    body = {
        'library_file_id': _as_int(file_id),
        'printer_id': _as_int(printer_id) if printer_id is not None else _printer_id(),
        'manual_start': bool(settings.BAMBUDDY_STAGED),
    }
    return _id(_request('POST', QUEUE_PATH, json=body), 'queue')


def start_item(queue_id):
    """Release a staged queue item: the plate is clear, print it next.

    Bambuddy answers 409 if the loaded spool looks short on filament, and 400
    if the item is no longer pending; both surface as BambuddyError.
    """
    _request('POST', START_PATH.format(item_id=queue_id))


def queue_item_event(queue_id):
    """Polling fallback: 'started'/'completed'/'failed' for a queue item's
    current status, None while it's pending, or 'missing' if Bambuddy no
    longer has it."""
    try:
        data = _request('GET', QUEUE_ITEM_PATH.format(item_id=queue_id))
    except BambuddyError as exc:
        if ' returned 404:' in str(exc):
            return 'missing'
        raise
    return QUEUE_STATUS_EVENTS.get(str(data.get('status', '')).lower())


def webhook_secret_ok(request):
    expected = settings.BAMBUDDY_WEBHOOK_SECRET
    if not expected:
        return False
    auth = request.headers.get('Authorization', '')
    given = auth[len('Bearer '):] if auth.startswith('Bearer ') else request.GET.get(WEBHOOK_SECRET_PARAM, '')
    return hmac.compare_digest(given.encode(), expected.encode())


def parse_webhook(payload):
    """Returns {'event': 'started'|'completed'|'failed', 'job_id', 'detail'},
    or None if the payload isn't a print event for a JohnCad job."""
    if not isinstance(payload, dict):
        return None
    event = WEBHOOK_EVENTS.get(payload.get('event'))
    if event is None:
        return None
    match = _JOB_NAME_RE.search(str(payload.get('filename') or ''))
    if match is None:
        return None
    detail = ''
    if event == 'failed':
        reason = payload.get('reason')
        detail = f'Bambuddy reported {payload["event"]}'
        detail += f': {reason}.' if reason and reason != 'Unknown' else '.'
    return {'event': event, 'job_id': int(match.group(1)), 'detail': detail}


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value
