"""Bambuddy REST API details and a small client, with no Django imports.

Shared by the Django side (jobs/bambuddy.py, which fills the settings in)
and by the print agent on the school Pi (agent/johncad_agent.py), which runs
without Django. Every Bambuddy-specific detail (paths, field names, header
names, status values) lives here so it can be corrected in one place.

Checked against Bambuddy's source (github.com/maziggy/bambuddy, commit
98ecaf5, 2026-10-22): backend/app/api/routes/library.py and print_queue.py,
schemas/library.py and print_queue.py, core/auth.py, and
services/notification_service.py. Not yet checked against a running
instance; Bambuddy's API browser is at <BAMBUDDY_URL>/docs.
"""
import re

import requests

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
# cancelled. Used for status polling; "pending" maps to no event.
QUEUE_STATUS_EVENTS = {
    'printing': 'started',
    'completed': 'completed',
    'failed': 'failed',
    'skipped': 'failed',
    'cancelled': 'failed',
}
# What queue_item_status() returns when Bambuddy no longer has the item.
MISSING = 'missing'

# Printers (https://wiki.bambuddy.cool/reference/api/, "Printers", read
# 2026-11-13; not yet run against a live Bambuddy). POST /printers takes
# {"name", "ip_address", "access_code", "serial_number"}. GET /printers lists
# [{"id", "name", "ip_address", "serial_number", "model", ...}].
# GET /printers/{id}/status has "connected": true once Bambuddy has reached the
# printer. DELETE /printers/{id}?delete_archives=false removes one. The API
# key needs permission to add printers and read their status.
PRINTERS_PATH = '/printers/'
PRINTER_PATH = '/printers/{printer_id}'
PRINTER_STATUS_PATH = '/printers/{printer_id}/status'

UPLOAD_NAME = 'johncad-job-{job_id}.gcode.3mf'
JOB_NAME_RE = re.compile(r'\bjohncad-job-(\d+)\b')


def upload_name(job_id):
    return UPLOAD_NAME.format(job_id=job_id)


class BambuddyNotConfigured(Exception):
    pass


class BambuddyError(Exception):
    pass


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


class BambuddyClient:
    def __init__(self, base_url, api_key, timeout=60):
        if not base_url or not api_key:
            raise BambuddyNotConfigured('BAMBUDDY_URL and BAMBUDDY_API_KEY must be set.')
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method, path, **kwargs):
        url = self.base_url + API_PREFIX + path
        try:
            response = requests.request(
                method,
                url,
                headers={API_KEY_HEADER: self.api_key},
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise BambuddyError(f'Could not reach Bambuddy at {self.base_url}: {exc}') from exc
        if response.status_code >= 400:
            raise BambuddyError(
                f'Bambuddy {method} {path} returned {response.status_code}: {response.text[:500]}'
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise BambuddyError(
                f'Bambuddy {method} {path} returned non-JSON: {response.text[:200]}'
            ) from exc

    @staticmethod
    def _id(data, what):
        if isinstance(data, dict) and data.get('id') not in (None, ''):
            return str(data['id'])
        raise BambuddyError(f'Bambuddy {what} response has no id: {data!r}'[:500])

    def upload_file(self, fileobj, filename):
        """Upload a sliced .gcode.3mf to Bambuddy's library. Returns its file id."""
        data = self._request('POST', UPLOAD_PATH, files={UPLOAD_FIELD: (filename, fileobj)})
        return self._id(data, 'upload')

    def enqueue(self, file_id, printer_id, manual_start):
        """Add a library file to a printer's queue. Returns the queue item id.
        With manual_start the item waits for start_item() (plate cleared)."""
        body = {
            'library_file_id': as_int(file_id),
            'printer_id': as_int(printer_id),
            'manual_start': bool(manual_start),
        }
        return self._id(self._request('POST', QUEUE_PATH, json=body), 'queue')

    def start_item(self, queue_id):
        """Release a staged queue item: the plate is clear, print it next.

        Bambuddy answers 409 if the loaded spool looks short on filament, and
        400 if the item is no longer pending; both raise BambuddyError.
        """
        self._request('POST', START_PATH.format(item_id=queue_id))

    def queue_item_status(self, queue_id):
        """The queue item's raw status (lowercase), or MISSING if Bambuddy no
        longer has it."""
        try:
            data = self._request('GET', QUEUE_ITEM_PATH.format(item_id=queue_id))
        except BambuddyError as exc:
            if ' returned 404:' in str(exc):
                return MISSING
            raise
        return str(data.get('status', '')).lower()

    def queue_item_event(self, queue_id):
        """'started'/'completed'/'failed' for a queue item's current status,
        None while it's pending, or MISSING if Bambuddy no longer has it."""
        status = self.queue_item_status(queue_id)
        if status == MISSING:
            return MISSING
        return QUEUE_STATUS_EVENTS.get(status)

    def add_printer(self, name, ip_address, access_code, serial_number):
        """Register a printer with Bambuddy. Use find_printer_id() afterwards
        for its id, and printer_connected() to see whether Bambuddy reached it."""
        self._request('POST', PRINTERS_PATH, json={
            'name': name,
            'ip_address': ip_address,
            'access_code': access_code,
            'serial_number': serial_number,
        })

    def find_printer_id(self, serial_number):
        """Bambuddy's id (an int) for the printer with this serial number, or
        None if it has no such printer."""
        printers = self._request('GET', PRINTERS_PATH)
        if not isinstance(printers, list):
            raise BambuddyError(f'Bambuddy printer list is not a list: {printers!r}'[:500])
        wanted = str(serial_number).strip().upper()
        for printer in printers:
            if not isinstance(printer, dict):
                continue
            if str(printer.get('serial_number', '')).strip().upper() == wanted:
                printer_id = as_int(printer.get('id'))
                if isinstance(printer_id, int):
                    return printer_id
        return None

    def printer_connected(self, printer_id):
        """True once Bambuddy has a live connection to the printer."""
        data = self._request('GET', PRINTER_STATUS_PATH.format(printer_id=printer_id))
        return isinstance(data, dict) and data.get('connected') is True

    def delete_printer(self, printer_id):
        """Remove a printer from Bambuddy (its print archives are kept)."""
        self._request(
            'DELETE', PRINTER_PATH.format(printer_id=printer_id),
            params={'delete_archives': 'false'},
        )
