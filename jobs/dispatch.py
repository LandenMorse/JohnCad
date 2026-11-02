"""Sending sliced jobs to Bambuddy and syncing their print status back.

Two ways to reach Bambuddy:
- Local (default): the `dispatch_worker` command, the Bambuddy webhook and
  the staff buttons call Bambuddy directly. All Bambuddy API details live in
  jobs/bambuddy_client.py.
- Print agent (BAMBUDDY_VIA_AGENT=True): this site is elsewhere (e.g. at
  home) and an agent next to Bambuddy pulls work from jobs/agent_api.py. The
  agent_* functions below are its side of the state machine.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
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
        printer_id = job.printer.bambuddy_printer_id if job.printer_id else None
        queue_id = bambuddy.enqueue(job.bambuddy_file_id, printer_id)
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
        release_requested_at=None,
        release_error='',
        working_at=None,
        failed_at=None,
    ))


def _staged_jobs():
    return (
        Job.objects.filter(status=Job.Status.QUEUED, released_at__isnull=True)
        .exclude(bambuddy_queue_id='')
        .select_related('printer', 'attachment', 'user')
        .order_by('dispatched_at', 'id')
    )


def next_staged_job(printer=None):
    """The oldest job waiting for the plate to be cleared on `printer` (None =
    jobs with no printer, which go to the BAMBUDDY_PRINTER_ID printer)."""
    return _staged_jobs().filter(printer=printer).first()


def next_staged_jobs():
    """The next staged job for each printer that has any, oldest first."""
    heads = {}
    for job in _staged_jobs():
        heads.setdefault(job.printer_id, job)
    return list(heads.values())


def release_next(printer=None):
    """Staff "plate cleared, release next" for one printer. Returns the
    released job, or None if nothing is waiting there. Raises bambuddy errors
    for the view to show.

    With a print agent this only records the request (the agent starts the
    print on its next poll); asking again before it has is a no-op.
    """
    job = next_staged_job(printer)
    if job is None:
        return None
    if settings.BAMBUDDY_VIA_AGENT:
        if job.release_requested_at is None:
            now = timezone.now()
            Job.objects.filter(pk=job.pk, release_requested_at__isnull=True).update(
                release_requested_at=now, release_error='',
            )
            job.release_requested_at = now
        return job
    bambuddy.start_item(job.bambuddy_queue_id)
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


def find_job_for_webhook(job_id):
    """The dispatched job a webhook is about, by the job id Bambuddy echoes
    back in the print's filename."""
    return Job.objects.filter(
        pk=job_id,
        status__in=[Job.Status.QUEUED, Job.Status.WORKING, Job.Status.FINISHED, Job.Status.FAILED],
    ).exclude(bambuddy_queue_id='').first()


def sync_from_bambuddy():
    """Polling fallback for missed webhooks: apply what Bambuddy's queue says
    about every in-flight job. Returns how many jobs changed."""
    jobs = (
        Job.objects.filter(status__in=[Job.Status.QUEUED, Job.Status.WORKING])
        .exclude(bambuddy_queue_id='')
    )
    changed = 0
    for job in jobs:
        event = bambuddy.queue_item_event(job.bambuddy_queue_id)
        if event == bambuddy.MISSING:
            logger.warning('Job #%s: Bambuddy queue item %s no longer exists.',
                           job.id, job.bambuddy_queue_id)
            continue
        if event is None:
            continue
        if apply_polled_event(job, event) == APPLIED:
            changed += 1
    return changed


def apply_polled_event(job, event, detail=''):
    """apply_event() for a status read from Bambuddy's queue rather than a
    webhook: polls can miss "started", so a job can go straight from queued
    to completed."""
    if event == 'completed' and job.status == Job.Status.QUEUED:
        apply_event(job, 'started')
    return apply_event(job, event, detail)


# --- Print agent (BAMBUDDY_VIA_AGENT) ---------------------------------------

def reset_stale_agent_claims():
    """Offer again jobs an agent claimed but stopped downloading. Every
    request for the job's file refreshes the claim."""
    cutoff = timezone.now() - timedelta(seconds=settings.AGENT_CLAIM_TIMEOUT)
    released = Job.objects.filter(
        status=Job.Status.QUEUED,
        bambuddy_queue_id='',
        dispatch_started_at__lt=cutoff,
    ).update(dispatch_started_at=None)
    if released:
        logger.warning('Released %s stale agent claim(s).', released)
    return released


def agent_dispatch_jobs(agent):
    """Sliced jobs waiting to go to `agent`'s Bambuddy: unclaimed ones, plus
    ones this agent already claimed (so it can resume after a restart)."""
    return (
        agent.jobs_in_scope()
        .filter(status=Job.Status.QUEUED, bambuddy_queue_id='', dispatch_error='')
        .filter(Q(dispatch_started_at__isnull=True) | Q(agent=agent))
        .exclude(sliced_file='')
        .select_related('printer')
        .order_by('approved_at', 'id')
    )


def agent_release_jobs(agent):
    """Staged jobs staff asked `agent` to start."""
    return (
        agent.jobs_in_scope()
        .filter(status=Job.Status.QUEUED, released_at__isnull=True,
                release_requested_at__isnull=False)
        .exclude(bambuddy_queue_id='')
        .order_by('release_requested_at', 'id')
    )


def agent_track_jobs(agent):
    """In-flight jobs whose Bambuddy queue item `agent` should report on."""
    return (
        agent.jobs_in_scope()
        .filter(status__in=[Job.Status.QUEUED, Job.Status.WORKING])
        .exclude(bambuddy_queue_id='')
        .order_by('dispatched_at', 'id')
    )


def agent_claim(agent, job):
    """Claim a job for `agent` to download and dispatch. Re-claiming a job it
    already holds just refreshes the claim. Returns False if the job isn't
    available to it (taken by another agent, dispatched, failed, ...)."""
    return bool(
        agent_dispatch_jobs(agent).filter(pk=job.pk).update(
            agent=agent, dispatch_started_at=timezone.now(),
        )
    )


def agent_holds_claim(agent, job):
    """True while `agent` may download this job's file; refreshes the claim."""
    return bool(
        Job.objects.filter(
            pk=job.pk,
            agent=agent,
            status=Job.Status.QUEUED,
            bambuddy_queue_id='',
            dispatch_started_at__isnull=False,
        ).update(dispatch_started_at=timezone.now())
    )


def agent_report_dispatched(job, file_id, queue_id):
    """The agent uploaded and enqueued the job. Returns APPLIED, UNCHANGED (a
    repeat of the same report) or CONFLICT."""
    if job.status == Job.Status.QUEUED and job.bambuddy_queue_id == queue_id:
        return UNCHANGED
    updated = Job.objects.filter(
        pk=job.pk, status=Job.Status.QUEUED, bambuddy_queue_id='',
    ).update(
        bambuddy_file_id=file_id,
        bambuddy_queue_id=queue_id,
        dispatched_at=timezone.now(),
        dispatch_started_at=None,
        dispatch_error='',
    )
    if not updated:
        return CONFLICT
    logger.info('Job #%s sent to Bambuddy by the print agent (queue item %s).', job.id, queue_id)
    return APPLIED


def agent_report_dispatch_failed(job, message, file_id=None):
    """The agent couldn't dispatch the job; shown on /review/ with Retry.
    `file_id` updates the stored Bambuddy file id ('' forgets it, so a retry
    uploads the file again). Returns False if the job had moved on."""
    fields = {'dispatch_error': message or 'The print agent could not send the job.',
              'dispatch_started_at': None}
    if file_id is not None:
        fields['bambuddy_file_id'] = file_id
    updated = Job.objects.filter(
        pk=job.pk, status=Job.Status.QUEUED, bambuddy_queue_id='',
    ).update(**fields)
    if updated:
        logger.error('Job #%s dispatch failed on the print agent: %s', job.id, message)
    return bool(updated)


def agent_report_released(job):
    """The agent started the staged print. Returns False if there was no
    release to confirm."""
    return bool(
        Job.objects.filter(
            pk=job.pk, status__in=[Job.Status.QUEUED, Job.Status.WORKING],
            released_at__isnull=True, release_requested_at__isnull=False,
        ).update(released_at=timezone.now(), release_error='')
    )


def agent_report_release_failed(job, message):
    """The agent couldn't start the print (e.g. Bambuddy thinks the spool is
    short). Clears the request so staff can try again from /queue/."""
    return bool(
        Job.objects.filter(
            pk=job.pk, status=Job.Status.QUEUED, released_at__isnull=True,
            release_requested_at__isnull=False,
        ).update(release_requested_at=None,
                 release_error=message or 'The print agent could not start the print.')
    )
