"""Sending sliced jobs to Bambuddy and syncing their print status back.

Used by the `dispatch_worker` command, the Bambuddy webhook and the staff
buttons. All Bambuddy API details live in jobs/bambuddy.py.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import bambuddy
from .models import Job

logger = logging.getLogger(__name__)

# Result of apply_event().
APPLIED = 'applied'
UNCHANGED = 'unchanged'
CONFLICT = 'conflict'

# event -> (states it may come from, new state, timestamp field). Anything
# else is a 409; an event for a job already in the new state is a no-op so
# repeated webhooks are harmless.
TRANSITIONS = {
    'started': ((Job.Status.QUEUED,), Job.Status.WORKING, 'working_at'),
    'completed': ((Job.Status.WORKING,), Job.Status.FINISHED, 'finished_at'),
    'failed': ((Job.Status.QUEUED, Job.Status.WORKING), Job.Status.FAILED, 'failed_at'),
}


def reset_stale_dispatch_claims():
    """Release jobs a crashed/killed worker claimed but never finished."""
    cutoff = timezone.now() - timedelta(seconds=settings.BAMBUDDY_TIMEOUT * 3 + 60)
    released = Job.objects.filter(
        status=Job.Status.QUEUED,
        dispatch_started_at__lt=cutoff,
    ).update(dispatch_started_at=None)
    if released:
        logger.warning('Released %s stale dispatch claim(s).', released)
    return released


def claim_next_dispatch():
    """Atomically claim the oldest sliced job not yet sent to Bambuddy, or None."""
    with transaction.atomic():
        job = (
            Job.objects.select_for_update(skip_locked=True)
            .filter(
                status=Job.Status.QUEUED,
                bambuddy_queue_id='',
                dispatch_error='',
                dispatch_started_at__isnull=True,
            )
            .exclude(sliced_file='')
            .order_by('approved_at', 'id')
            .first()
        )
        if job is not None:
            job.dispatch_started_at = timezone.now()
            job.save(update_fields=['dispatch_started_at'])
        return job


def dispatch_claimed_job(job):
    """Upload and enqueue a job returned by claim_next_dispatch().

    Returns True on success. On failure the error is stored on the job (shown
    on the review page) and it isn't retried until a teacher clicks Retry.
    """
    try:
        if not job.bambuddy_file_id:
            with job.sliced_file.open('rb') as f:
                file_id = bambuddy.upload_file(f, bambuddy.upload_name(job.id))
            # Saved straight away so a failed enqueue doesn't re-upload on retry.
            Job.objects.filter(pk=job.pk).update(bambuddy_file_id=file_id)
            job.bambuddy_file_id = file_id
        queue_id = bambuddy.enqueue(job.bambuddy_file_id)
    except (bambuddy.BambuddyNotConfigured, bambuddy.BambuddyError) as exc:
        logger.error('Job #%s dispatch failed: %s', job.id, exc)
        _record_dispatch_error(job, str(exc) or exc.__class__.__name__)
        return False
    except Exception:
        logger.exception('Job #%s dispatch crashed unexpectedly.', job.id)
        _record_dispatch_error(job, 'Unexpected error while dispatching; see the server log.')
        return False
    Job.objects.filter(pk=job.pk).update(
        bambuddy_queue_id=queue_id,
        dispatched_at=timezone.now(),
        dispatch_started_at=None,
    )
    logger.info('Job #%s sent to Bambuddy (queue item %s).', job.id, queue_id)
    return True


def _record_dispatch_error(job, message):
    Job.objects.filter(pk=job.pk).update(dispatch_error=message, dispatch_started_at=None)


def retry_dispatch(job):
    """Teacher Retry: re-send a job whose dispatch or print failed.

    Returns False if the job isn't in a retryable state.
    """
    if Job.objects.filter(
        pk=job.pk, status=Job.Status.QUEUED, dispatch_started_at__isnull=True,
    ).exclude(dispatch_error='').update(dispatch_error=''):
        return True
    # A failed print goes back into the queue as a new Bambuddy queue item,
    # reusing the uploaded file.
    return bool(Job.objects.filter(pk=job.pk, status=Job.Status.FAILED).update(
        status=Job.Status.QUEUED,
        dispatch_error='',
        bambuddy_queue_id='',
        dispatched_at=None,
        released_at=None,
        working_at=None,
        failed_at=None,
    ))


def next_staged_job():
    """The oldest job sitting in Bambuddy waiting for the plate to be cleared."""
    return (
        Job.objects.filter(status=Job.Status.QUEUED, released_at__isnull=True)
        .exclude(bambuddy_queue_id='')
        .order_by('dispatched_at', 'id')
        .first()
    )


def release_next():
    """Staff "plate cleared, release next". Returns the released job, or None
    if nothing is waiting. Raises bambuddy errors for the view to show."""
    job = next_staged_job()
    if job is None:
        return None
    bambuddy.release_next()
    Job.objects.filter(pk=job.pk).update(released_at=timezone.now())
    return job


def apply_event(job, event, detail=''):
    """Move a job along for a Bambuddy print event. Returns APPLIED,
    UNCHANGED (already there) or CONFLICT (not a valid transition)."""
    sources, target, stamp = TRANSITIONS[event]
    if job.status == target:
        return UNCHANGED
    fields = {'status': target, stamp: timezone.now()}
    if target == Job.Status.FAILED:
        fields['dispatch_error'] = detail or 'The print failed in Bambuddy.'
    if not Job.objects.filter(pk=job.pk, status__in=sources).update(**fields):
        return CONFLICT
    for name, value in fields.items():
        setattr(job, name, value)
    logger.info('Job #%s is now %s (Bambuddy %s).', job.id, target, event)
    return APPLIED


def find_job_for_webhook(job_id, archive_id):
    """The in-flight job a webhook is about: by the job id in the uploaded
    filename if present, else the oldest in-flight job with that file id."""
    in_flight = Job.objects.filter(
        status__in=[Job.Status.QUEUED, Job.Status.WORKING, Job.Status.FINISHED, Job.Status.FAILED],
    ).exclude(bambuddy_queue_id='')
    if job_id is not None:
        return in_flight.filter(pk=job_id).first()
    if archive_id is not None:
        return in_flight.filter(bambuddy_file_id=archive_id).order_by(
            'dispatched_at', 'id',
        ).first()
    return None


def sync_from_bambuddy():
    """Polling fallback for missed webhooks: apply what Bambuddy's queue says
    about every in-flight job. Returns how many jobs changed."""
    jobs = list(
        Job.objects.filter(status__in=[Job.Status.QUEUED, Job.Status.WORKING])
        .exclude(bambuddy_queue_id='')
    )
    if not jobs:
        return 0
    events = bambuddy.queue_events()
    changed = 0
    for job in jobs:
        event = events.get(job.bambuddy_queue_id)
        if event is None:
            continue
        # A missed "started" means a completed job may still be queued here.
        if event == 'completed' and job.status == Job.Status.QUEUED:
            apply_event(job, 'started')
        if apply_event(job, event) == APPLIED:
            changed += 1
    return changed
