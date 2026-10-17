"""Thin client for the self-hosted Bambuddy REST API.

Django never talks to printers; everything goes through Bambuddy. Every
Bambuddy-specific detail (paths, field names, header names, status values)
is kept in this module so it can be corrected in one place.

UNVERIFIED: the constants marked TODO(verify) come from Bambuddy's wiki
(wiki.bambuddy.cool, "API reference" / "API keys" pages), not from running it.
Before relying on dispatch, open Bambuddy's built-in API browser on the Pi
(<BAMBUDDY_URL>/docs) and confirm each one; see docs/BAMBUDDY_PLAN.md.
"""
import hmac
import re

import requests
from django.conf import settings

API_PREFIX = '/api/v1'
# Documented in the wiki's API-keys page (curl -H "X-API-Key: ...").
API_KEY_HEADER = 'X-API-Key'

# TODO(verify): upload path, multipart field name and which response field
# holds the new file's id. The wiki lists POST /library/files/upload but
# documents no body or response.
UPLOAD_PATH = '/library/files/upload'
UPLOAD_FIELD = 'file'
UPLOAD_ID_FIELDS = ('id', 'file_id', 'archive_id')

# The wiki's documented queue body is {"archive_id", "printer_id",
# "scheduled_time"}. TODO(verify): whether a library upload's id goes in
# archive_id or another field, how "Queue Only"/manual start is requested
# (QUEUE_STAGED_FIELD), and the response's id field.
QUEUE_PATH = '/queue'
QUEUE_FILE_FIELD = 'archive_id'
QUEUE_STAGED_FIELD = 'manual_start'
QUEUE_ID_FIELDS = ('id', 'queue_id')

# Documented: "Start the next pending job in this printer's queue, releasing
# it if it waits for a manual start". Per-printer, not per-item.
RELEASE_PATH = '/webhook/printer/{printer_id}/start'

# Polling fallback: no GET /queue/<id> is documented, only a filtered list.
# TODO(verify): the API's status values.
QUEUE_STATUS_EVENTS = {
    'printing': 'started',
    'completed': 'completed',
    'failed': 'failed',
    'cancelled': 'failed',
}

# Outgoing webhooks (Settings > Notifications). Documented payload:
# {"event": "print_complete", "data": {"printer_id", "archive_id", "status", ...}}
# There is no queue item id and no signing, so jobs are matched on the
# uploaded filename (which carries the job id, see upload_name) and then on
# archive_id; the shared secret travels in the URL or a header.
WEBHOOK_EVENTS = {
    'print_started': 'started',
    'print_complete': 'completed',
    'print_failed': 'failed',
    'print_stopped': 'failed',
}
WEBHOOK_SECRET_HEADER = 'X-Webhook-Secret'
WEBHOOK_SECRET_PARAM = 'secret'


UPLOAD_NAME = 'johncad-job-{job_id}.gcode.3mf'
_UPLOAD_NAME_RE = re.compile(r'johncad-job-(\d+)\.gcode\.3mf')


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
    return settings.BAMBUDDY_PRINTER_ID


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


def _pick_id(data, fields, what):
    for field in fields:
        if isinstance(data, dict) and data.get(field) not in (None, ''):
            return str(data[field])
    raise BambuddyError(f'Bambuddy {what} response has no id ({", ".join(fields)}): {data!r}'[:500])


def upload_file(fileobj, filename):
    """Upload a sliced .gcode.3mf. Returns Bambuddy's id for it."""
    data = _request('POST', UPLOAD_PATH, files={UPLOAD_FIELD: (filename, fileobj)})
    return _pick_id(data, UPLOAD_ID_FIELDS, 'upload')


def enqueue(file_id):
    """Add an uploaded file to the printer's queue. Returns the queue item id.

    With BAMBUDDY_STAGED the item waits for release_next() (bed clear).
    """
    body = {QUEUE_FILE_FIELD: _as_int(file_id), 'printer_id': _as_int(_printer_id())}
    if settings.BAMBUDDY_STAGED:
        body[QUEUE_STAGED_FIELD] = True
    data = _request('POST', QUEUE_PATH, json=body)
    return _pick_id(data, QUEUE_ID_FIELDS, 'queue')


def release_next():
    """Tell Bambuddy the plate is clear: start the printer's next waiting item."""
    _request('POST', RELEASE_PATH.format(printer_id=_printer_id()))


def queue_events():
    """Polling fallback: {queue item id: 'started'|'completed'|'failed'} for
    the printer's queue items whose status maps to an event."""
    data = _request('GET', QUEUE_PATH, params={'printer_id': _printer_id()})
    items = data.get('items', []) if isinstance(data, dict) else data
    events = {}
    for item in items or []:
        if not isinstance(item, dict) or item.get('id') is None:
            continue
        event = QUEUE_STATUS_EVENTS.get(str(item.get('status', '')).lower())
        if event:
            events[str(item['id'])] = event
    return events


def webhook_secret_ok(request):
    expected = settings.BAMBUDDY_WEBHOOK_SECRET
    if not expected:
        return False
    given = request.headers.get(WEBHOOK_SECRET_HEADER) or request.GET.get(WEBHOOK_SECRET_PARAM, '')
    return hmac.compare_digest(given.encode(), expected.encode())


def parse_webhook(payload):
    """Returns a dict with event ('started'/'completed'/'failed'), job_id
    (from the filename, or None), archive_id (or None) and detail, or None if
    the payload isn't a print event JohnCad tracks."""
    if not isinstance(payload, dict):
        return None
    event = WEBHOOK_EVENTS.get(payload.get('event'))
    data = payload.get('data')
    if event is None or not isinstance(data, dict):
        return None
    match = _UPLOAD_NAME_RE.search(str(data.get('filename') or ''))
    archive_id = data.get('archive_id')
    if match is None and archive_id in (None, ''):
        return None
    # print_complete carries data.status; anything but success is a failure.
    status = str(data.get('status') or 'success').lower()
    if event == 'completed' and status not in ('success', 'completed'):
        event = 'failed'
    return {
        'event': event,
        'job_id': int(match.group(1)) if match else None,
        'archive_id': None if archive_id in (None, '') else str(archive_id),
        'detail': f'Bambuddy reported {payload.get("event")} ({status}).' if event == 'failed' else '',
    }


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value
