import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Job
from .slicing import SlicerNotConfigured, SlicingFailed, slice_to_3mf

logger = logging.getLogger(__name__)


def jobs_visible_to(user):
    """Jobs `user` may review: all for superusers; otherwise jobs submitted to
    a class they teach, plus their own."""
    if user.is_superuser:
        return Job.objects.all()
    return Job.objects.filter(Q(classroom__teachers=user) | Q(user=user)).distinct()


def approve_job(job):
    """Approve a pending job, or re-queue one whose slicing failed.

    Slicing itself happens later in the `slice_worker` command, so this returns
    immediately. Returns False if the job wasn't in a state that can be
    (re)approved, e.g. it's already being sliced or was rejected.
    """
    approved = Job.objects.filter(pk=job.pk, status=Job.Status.PENDING).update(
        status=Job.Status.APPROVED,
        approved_at=timezone.now(),
        slicing_error='',
    )
    if approved:
        return True
    # Retry: only a job whose slice failed, never one a worker is slicing.
    return bool(
        Job.objects.filter(
            pk=job.pk,
            status=Job.Status.APPROVED,
            slicing_started_at__isnull=True,
        ).exclude(slicing_error='').update(slicing_error='')
    )


def reject_job(job):
    """Reject a pending job. Returns False if it was no longer pending."""
    updated = Job.objects.filter(pk=job.pk, status=Job.Status.PENDING).update(
        status=Job.Status.REJECTED,
    )
    return bool(updated)


def reset_stale_claims():
    """Release jobs a crashed/killed worker claimed but never finished."""
    cutoff = timezone.now() - timedelta(seconds=settings.ORCA_SLICER_TIMEOUT + 60)
    released = Job.objects.filter(
        status=Job.Status.APPROVED,
        slicing_started_at__lt=cutoff,
    ).update(slicing_started_at=None)
    if released:
        logger.warning('Released %s stale slicing claim(s).', released)
    return released


def claim_next_job():
    """Atomically claim the oldest approved job awaiting slicing, or None."""
    with transaction.atomic():
        job = (
            Job.objects.select_for_update(skip_locked=True)
            .select_related('attachment')
            .filter(
                status=Job.Status.APPROVED,
                sliced_file='',
                slicing_error='',
                slicing_started_at__isnull=True,
            )
            .order_by('approved_at', 'id')
            .first()
        )
        if job is not None:
            job.slicing_started_at = timezone.now()
            job.save(update_fields=['slicing_started_at'])
        return job


def slice_claimed_job(job):
    """Slice a job returned by claim_next_job(). Returns True on success.

    On failure the error is stored on the job (shown on the review page) and
    the job stays "approved" until a teacher retries it.
    """
    try:
        slice_to_3mf(job)
    except (SlicerNotConfigured, SlicingFailed) as exc:
        if isinstance(exc, SlicerNotConfigured):
            logger.warning('Job #%s not sliced, slicer not configured: %s', job.id, exc)
        else:
            logger.error('Job #%s slicing failed: %s', job.id, exc)
        Job.objects.filter(pk=job.pk).update(
            slicing_error=str(exc) or exc.__class__.__name__,
            slicing_started_at=None,
        )
        return False
    except Exception:
        logger.exception('Job #%s slicing crashed unexpectedly.', job.id)
        Job.objects.filter(pk=job.pk).update(
            slicing_error='Unexpected error while slicing; see the server log.',
            slicing_started_at=None,
        )
        return False
    Job.objects.filter(pk=job.pk).update(slicing_started_at=None)
    logger.info('Job #%s sliced and queued.', job.id)
    return True
