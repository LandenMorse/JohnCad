"""HTTPS API for the print agent on the printers' network (agent/README.md).

Used when this site isn't next to Bambuddy (BAMBUDDY_VIA_AGENT=True): the
agent polls here for work, downloads sliced files in resumable chunks (HTTP
Range requests), hands them to its local Bambuddy and reports back. The
agent only ever makes outbound HTTPS requests, so the printers' network needs
no inbound access.

Every request carries the agent's key as "Authorization: Bearer jca_...".
Served under /agent-api/v1/, which the Caddyfile leaves public (unlike
/api/, which holds the local-only Bambuddy webhook).
"""
import hashlib
import json
import re
from functools import wraps

from django.conf import settings
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import dispatch
from .bambuddy_client import upload_name
from .models import Job, PrinterAgent

API_VERSION = 1
STREAM_BLOCK = 64 * 1024
MAX_MESSAGE = 2000
_RANGE_RE = re.compile(r'^bytes=(\d*)-(\d*)$')
_EVENTS = set(dispatch.TRANSITIONS)


def _error(message, status):
    return JsonResponse({'error': message}, status=status)


def agent_required(view):
    """Authenticate the agent key; 404 everything if agent mode is off so the
    API doesn't exist on local setups."""
    @csrf_exempt
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not settings.BAMBUDDY_VIA_AGENT:
            raise Http404('The print agent API is off (BAMBUDDY_VIA_AGENT).')
        auth = request.headers.get('Authorization', '')
        key = auth[len('Bearer '):].strip() if auth.startswith('Bearer ') else ''
        agent = PrinterAgent.for_key(key)
        if agent is None:
            return _error('Bad or missing agent key.', 401)
        PrinterAgent.objects.filter(pk=agent.pk).update(last_seen_at=timezone.now())
        request.agent = agent
        return view(request, *args, **kwargs)
    return wrapper


def _body(request):
    """The JSON request body as a dict ({} if empty); None if it isn't JSON."""
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _text(value):
    return str(value or '').strip()[:MAX_MESSAGE]


def _agent_job(request, pk):
    return get_object_or_404(request.agent.jobs_in_scope(), pk=pk)


def _printer_id(job):
    """Bambuddy's id for the job's printer; None = the agent's own default."""
    return job.printer.bambuddy_printer_id if job.printer_id else None


def _sha256(field_file):
    digest = hashlib.sha256()
    with field_file.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


@require_GET
@agent_required
def poll(request):
    """Everything the agent should do now."""
    agent = request.agent
    dispatch.reset_stale_agent_claims()
    return JsonResponse({
        'api_version': API_VERSION,
        'agent': agent.name,
        'poll_interval': settings.AGENT_POLL_INTERVAL,
        'staged': settings.BAMBUDDY_STAGED,
        'dispatch': [
            {
                'id': job.id,
                'printer_id': _printer_id(job),
                'bambuddy_file_id': job.bambuddy_file_id,
                'claimed': job.agent_id == agent.id and job.dispatch_started_at is not None,
            }
            for job in dispatch.agent_dispatch_jobs(agent)
        ],
        'release': [
            {'id': job.id, 'bambuddy_queue_id': job.bambuddy_queue_id}
            for job in dispatch.agent_release_jobs(agent)
        ],
        'track': [
            {'id': job.id, 'status': job.status, 'bambuddy_queue_id': job.bambuddy_queue_id}
            for job in dispatch.agent_track_jobs(agent)
        ],
    })


@require_POST
@agent_required
def claim(request, pk):
    """Claim a job and get what's needed to download and enqueue it."""
    job = _agent_job(request, pk)
    if not dispatch.agent_claim(request.agent, job):
        return _error('Job is not available to dispatch.', 409)
    job.refresh_from_db()
    try:
        size = job.sliced_file.size
        sha256 = _sha256(job.sliced_file)
    except OSError:
        dispatch.agent_report_dispatch_failed(job, 'The sliced file is missing on the server.')
        return _error('The sliced file is missing on the server.', 410)
    return JsonResponse({
        'id': job.id,
        'file_name': upload_name(job.id),
        'size': size,
        'sha256': sha256,
        'printer_id': _printer_id(job),
        'bambuddy_file_id': job.bambuddy_file_id,
    })


def _parse_range(header, size):
    """(start, end) inclusive for a single "bytes=a-b" range, None for no
    Range header, or 'invalid'."""
    if not header:
        return None
    match = _RANGE_RE.match(header.strip())
    if match is None or match.groups() == ('', ''):
        return 'invalid'
    first, last = match.groups()
    if first == '':                       # bytes=-N: the last N bytes
        length = int(last)
        if length == 0:
            return 'invalid'
        return max(size - length, 0), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        return 'invalid'
    return start, end


def _stream(field_file, start, length):
    with field_file.open('rb') as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            block = f.read(min(STREAM_BLOCK, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block


@require_GET
@agent_required
def download(request, pk):
    """The job's sliced file. Supports one "Range: bytes=a-b" per request so
    the agent can fetch it in chunks and resume after a dropped connection.
    Each request keeps the agent's claim fresh."""
    job = _agent_job(request, pk)
    if not dispatch.agent_holds_claim(request.agent, job):
        return _error('Claim the job before downloading it.', 409)
    try:
        size = job.sliced_file.size
    except OSError:
        return _error('The sliced file is missing on the server.', 410)

    byte_range = _parse_range(request.headers.get('Range', ''), size)
    if byte_range == 'invalid':
        response = _error('Bad or unsatisfiable Range.', 416)
        response['Content-Range'] = f'bytes */{size}'
        return response
    start, end = byte_range if byte_range else (0, size - 1)
    length = max(end - start + 1, 0)
    response = StreamingHttpResponse(
        _stream(job.sliced_file, start, length),
        status=206 if byte_range else 200,
        content_type='application/octet-stream',
    )
    response['Content-Length'] = str(length)
    response['Accept-Ranges'] = 'bytes'
    response['Content-Disposition'] = f'attachment; filename="{upload_name(job.id)}"'
    if byte_range:
        response['Content-Range'] = f'bytes {start}-{end}/{size}'
    return response


@require_POST
@agent_required
def dispatched(request, pk):
    """{"bambuddy_file_id", "bambuddy_queue_id"}: the job is in Bambuddy's queue."""
    job = _agent_job(request, pk)
    data = _body(request)
    if data is None:
        return _error('Body must be a JSON object.', 400)
    file_id = _text(data.get('bambuddy_file_id'))[:64]
    queue_id = _text(data.get('bambuddy_queue_id'))[:64]
    if not file_id or not queue_id:
        return _error('bambuddy_file_id and bambuddy_queue_id are required.', 400)
    result = dispatch.agent_report_dispatched(job, file_id, queue_id)
    if result == dispatch.CONFLICT:
        return _error(f'Job is "{job.status}" and can\'t take a new queue item.', 409)
    if result == dispatch.APPLIED:
        Job.objects.filter(pk=job.pk).update(agent=request.agent)
    return JsonResponse({'status': 'ok' if result == dispatch.APPLIED else 'unchanged'})


@require_POST
@agent_required
def dispatch_failed(request, pk):
    """{"error", optional "bambuddy_file_id"}: the agent couldn't dispatch it."""
    job = _agent_job(request, pk)
    data = _body(request)
    if data is None:
        return _error('Body must be a JSON object.', 400)
    file_id = data.get('bambuddy_file_id')
    file_id = None if file_id is None else _text(file_id)[:64]
    if not dispatch.agent_report_dispatch_failed(job, _text(data.get('error')), file_id):
        return _error('Job is no longer waiting to be dispatched.', 409)
    return JsonResponse({'status': 'ok'})


@require_POST
@agent_required
def released(request, pk):
    """The agent started the staged print."""
    job = _agent_job(request, pk)
    if not dispatch.agent_report_released(job):
        return _error('No release was waiting for this job.', 409)
    return JsonResponse({'status': 'ok'})


@require_POST
@agent_required
def release_failed(request, pk):
    """{"error"}: the agent couldn't start the print."""
    job = _agent_job(request, pk)
    data = _body(request)
    if data is None:
        return _error('Body must be a JSON object.', 400)
    if not dispatch.agent_report_release_failed(job, _text(data.get('error'))):
        return _error('No release was waiting for this job.', 409)
    return JsonResponse({'status': 'ok'})


@require_POST
@agent_required
def event(request, pk):
    """{"event": "started"|"completed"|"failed", optional "detail"}: what
    Bambuddy's queue says about the job's print."""
    job = _agent_job(request, pk)
    data = _body(request)
    if data is None:
        return _error('Body must be a JSON object.', 400)
    name = data.get('event')
    if name not in _EVENTS:
        return _error(f'event must be one of {sorted(_EVENTS)}.', 400)
    if not job.bambuddy_queue_id:
        return _error('Job has not been dispatched.', 409)
    result = dispatch.apply_polled_event(job, name, _text(data.get('detail')))
    if result == dispatch.CONFLICT:
        return _error(f'Job is "{job.status}", can\'t apply "{name}".', 409)
    return JsonResponse({'status': 'ok' if result == dispatch.APPLIED else 'unchanged'})
