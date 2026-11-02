"""Django-side wrapper around the Bambuddy client, plus the webhook parser.

Django never talks to printers; everything goes through Bambuddy. The API
details themselves (paths, field names, status values) live in
jobs/bambuddy_client.py, which the school-Pi print agent shares; this module
fills in the settings and parses Bambuddy's webhook.
"""
import hmac

import requests  # noqa: F401  (tests patch jobs.bambuddy.requests.request)
from django.conf import settings

from .bambuddy_client import (  # noqa: F401  (re-exported for callers)
    API_KEY_HEADER,
    API_PREFIX,
    JOB_NAME_RE,
    MISSING,
    QUEUE_STATUS_EVENTS,
    BambuddyClient,
    BambuddyError,
    BambuddyNotConfigured,
    as_int,
    upload_name,
)

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


def _client():
    return BambuddyClient(
        settings.BAMBUDDY_URL, settings.BAMBUDDY_API_KEY, settings.BAMBUDDY_TIMEOUT,
    )


def _printer_id():
    if not settings.BAMBUDDY_PRINTER_ID:
        raise BambuddyNotConfigured('BAMBUDDY_PRINTER_ID is not set.')
    return as_int(settings.BAMBUDDY_PRINTER_ID)


def upload_file(fileobj, filename):
    """Upload a sliced .gcode.3mf to Bambuddy's library. Returns its file id."""
    return _client().upload_file(fileobj, filename)


def enqueue(file_id, printer_id=None):
    """Add an uploaded library file to a printer's queue. Returns the queue
    item id. `printer_id` is Bambuddy's id for the job's printer; without one
    the BAMBUDDY_PRINTER_ID setting is used. With BAMBUDDY_STAGED the item
    waits for start_item() (bed clear).
    """
    client = _client()
    if printer_id is None:
        printer_id = _printer_id()
    return client.enqueue(file_id, printer_id, settings.BAMBUDDY_STAGED)


def start_item(queue_id):
    """Release a staged queue item (see BambuddyClient.start_item)."""
    _client().start_item(queue_id)


def queue_item_event(queue_id):
    """Polling fallback: 'started'/'completed'/'failed' for a queue item's
    current status, None while it's pending, or 'missing' if Bambuddy no
    longer has it."""
    return _client().queue_item_event(queue_id)


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
    match = JOB_NAME_RE.search(str(payload.get('filename') or ''))
    if match is None:
        return None
    detail = ''
    if event == 'failed':
        reason = payload.get('reason')
        detail = f'Bambuddy reported {payload["event"]}'
        detail += f': {reason}.' if reason and reason != 'Unknown' else '.'
    return {'event': event, 'job_id': int(match.group(1)), 'detail': detail}

