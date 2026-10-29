from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.permissions import classes_visible_to, staff_required
from attachments.models import Attachment

from .bambuddy import BambuddyError, BambuddyNotConfigured
from .dispatch import next_staged_jobs, release_next, retry_dispatch
from .models import Job, Printer
from .services import approve_job, jobs_visible_to, reject_job


@login_required
def attachment_export(request, pk):
    attachment = get_object_or_404(Attachment, pk=pk, user=request.user)
    if request.method == 'POST':
        classes = list(request.user.classes.all())
        if len(classes) == 1:
            classroom = classes[0]
        elif not classes:
            classroom = None
        else:
            chosen = request.POST.get('classroom', '')
            classroom = next((c for c in classes if str(c.pk) == chosen), None)
            if classroom is None:
                messages.error(request, f'Choose which class to submit "{attachment.name}" to.')
                return redirect('attachment-list')
        Job.objects.create(user=request.user, attachment=attachment, classroom=classroom)
        where = f' to {classroom.name}' if classroom else ''
        messages.success(request, f'Export requested for "{attachment.name}"{where}.')
    return redirect('attachment-list')


@login_required
def queue(request):
    jobs = Job.objects.filter(
        status__in=[Job.Status.QUEUED, Job.Status.WORKING],
    ).select_related('user', 'attachment', 'printer').order_by('approved_at')
    return render(request, 'jobs/queue.html', {
        'jobs': jobs,
        'next_staged': next_staged_jobs() if request.user.is_staff else [],
    })


@staff_required
@require_POST
def queue_release_next(request):
    """Staff confirm one printer's plate is clear; Bambuddy starts that
    printer's next staged job."""
    chosen = request.POST.get('printer', '')
    printer = None
    if chosen:
        printer = Printer.objects.filter(pk=chosen).first() if chosen.isdigit() else None
        if printer is None:
            messages.error(request, 'That printer no longer exists.')
            return redirect('queue')
    try:
        job = release_next(printer)
    except (BambuddyError, BambuddyNotConfigured) as exc:
        messages.error(request, f'Could not release the next job: {exc}')
        return redirect('queue')
    if job is None:
        messages.error(request, 'No job is waiting for the plate to be cleared.')
    else:
        where = f' on {job.printer.name}' if job.printer_id else ''
        messages.success(request, f'Released "{job.attachment.name}"{where}.')
    return redirect('queue')


@staff_required
def review(request):
    classes = list(classes_visible_to(request.user))
    selected = request.GET.get('classroom', '')
    jobs = jobs_visible_to(request.user).select_related(
        'user', 'attachment', 'classroom', 'printer',
    ).order_by('-created_at')
    if selected == 'none' and request.user.is_superuser:
        jobs = jobs.filter(classroom__isnull=True)
    elif selected.isdigit() and any(c.pk == int(selected) for c in classes):
        jobs = jobs.filter(classroom_id=int(selected))
    else:
        selected = ''
    jobs = list(jobs)
    return render(request, 'jobs/review.html', {
        'jobs': jobs,
        'classes': classes,
        'printers': list(Printer.objects.filter(is_active=True)),
        'selected_classroom': selected,
        'auto_refresh': any(
            job.slicing_state in ('waiting', 'slicing')
            or job.dispatch_state in ('waiting', 'dispatching')
            for job in jobs
        ),
        'pending_count': sum(job.status == Job.Status.PENDING for job in jobs),
    })


def _back_to_review(request):
    """Redirect to /review/, keeping the class filter the action was taken under."""
    url = reverse('job-review')
    classroom = request.POST.get('classroom', '')
    if classroom:
        url += '?' + urlencode({'classroom': classroom})
    return redirect(url)


@staff_required
@require_POST
def review_approve(request, pk):
    job = get_object_or_404(jobs_visible_to(request.user), pk=pk)
    printer = None
    if Printer.objects.filter(is_active=True).exists():
        chosen = request.POST.get('printer', '')
        if chosen.isdigit():
            printer = Printer.objects.filter(pk=chosen, is_active=True).first()
        if printer is None:
            messages.error(request, f'Choose a printer for job #{job.id}.')
            return _back_to_review(request)
    if approve_job(job, printer):
        where = f' for {printer.name}' if printer else ''
        messages.success(
            request,
            f'Job #{job.id} approved{where}. It is being sliced and will join the '
            'print queue when ready.',
        )
    else:
        messages.error(request, f'Job #{job.id} can no longer be approved or retried.')
    return _back_to_review(request)


@staff_required
@require_POST
def review_reject(request, pk):
    job = get_object_or_404(jobs_visible_to(request.user), pk=pk)
    if reject_job(job):
        messages.success(request, f'Job #{job.id} rejected.')
    else:
        messages.error(request, f'Job #{job.id} is no longer pending.')
    return _back_to_review(request)


@staff_required
@require_POST
def review_retry_dispatch(request, pk):
    job = get_object_or_404(jobs_visible_to(request.user), pk=pk)
    if retry_dispatch(job):
        messages.success(request, f'Job #{job.id} will be sent to the printer again.')
    else:
        messages.error(request, f'Job #{job.id} can no longer be retried.')
    return _back_to_review(request)
