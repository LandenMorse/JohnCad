from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from attachments.models import Attachment

from .models import Job
from .services import approve_job, reject_job


@login_required
def attachment_export(request, pk):
    attachment = get_object_or_404(Attachment, pk=pk, user=request.user)
    if request.method == 'POST':
        Job.objects.create(user=request.user, attachment=attachment)
        messages.success(request, f'Export requested for "{attachment.name}".')
    return redirect('attachment-list')


@login_required
def queue(request):
    jobs = Job.objects.filter(
        status__in=[Job.Status.QUEUED, Job.Status.WORKING],
    ).select_related('user', 'attachment').order_by('approved_at')
    return render(request, 'jobs/queue.html', {'jobs': jobs})


def staff_required(view):
    """Staff (teachers) only: anonymous -> login page, non-staff -> 403."""
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


@staff_required
def review(request):
    jobs = list(Job.objects.select_related('user', 'attachment').order_by('-created_at'))
    return render(request, 'jobs/review.html', {
        'jobs': jobs,
        'auto_refresh': any(job.slicing_state in ('waiting', 'slicing') for job in jobs),
        'pending_count': sum(job.status == Job.Status.PENDING for job in jobs),
    })


@staff_required
@require_POST
def review_approve(request, pk):
    job = get_object_or_404(Job, pk=pk)
    if approve_job(job):
        messages.success(
            request,
            f'Job #{job.id} approved. It is being sliced and will join the print '
            'queue when ready.',
        )
    else:
        messages.error(request, f'Job #{job.id} can no longer be approved or retried.')
    return redirect('job-review')


@staff_required
@require_POST
def review_reject(request, pk):
    job = get_object_or_404(Job, pk=pk)
    if reject_job(job):
        messages.success(request, f'Job #{job.id} rejected.')
    else:
        messages.error(request, f'Job #{job.id} is no longer pending.')
    return redirect('job-review')
