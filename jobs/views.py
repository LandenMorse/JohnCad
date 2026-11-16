import os
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.permissions import classes_visible_to, staff_required
from attachments.models import Attachment

from .bambuddy import BambuddyError, BambuddyNotConfigured
from . import printer_tickets
from .dispatch import next_staged_jobs, release_next, retry_dispatch
from .forms import AddPrinterTicketForm, JobApprovalForm, PrinterForm, SlicerTemplateForm
from .models import AddPrinterTicket, Job, Printer, PrinterAgent, SlicerTemplate
from .services import approve_job, delete_template, jobs_visible_to, reject_job


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
    agents = []
    if request.user.is_staff and settings.BAMBUDDY_VIA_AGENT:
        agents = list(PrinterAgent.objects.filter(is_active=True))
    return render(request, 'jobs/queue.html', {
        'jobs': jobs,
        'next_staged': next_staged_jobs() if request.user.is_staff else [],
        'via_agent': settings.BAMBUDDY_VIA_AGENT,
        'agents': agents,
        'agent_offline_after': settings.AGENT_POLL_INTERVAL * 4 + 30,
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
        if settings.BAMBUDDY_VIA_AGENT:
            messages.success(
                request,
                f'Release of "{job.attachment.name}"{where} requested; the print '
                'agent will start it on its next check-in.',
            )
        else:
            messages.success(request, f'Released "{job.attachment.name}"{where}.')
    return redirect('queue')


@staff_required
def review(request):
    classes = list(classes_visible_to(request.user))
    selected = request.GET.get('classroom', '')
    jobs = jobs_visible_to(request.user).select_related(
        'user', 'attachment', 'classroom', 'printer', 'template',
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
        'printers': JobApprovalForm.approval_choices(),
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
    form = JobApprovalForm(request.POST)
    if not form.is_valid():
        messages.error(request, f'Job #{job.id}: {form.errors["target"][0]}')
        return _back_to_review(request)
    printer, template = form.cleaned_data['printer'], form.cleaned_data['template']
    if approve_job(job, printer, template):
        messages.success(
            request,
            f'Job #{job.id} approved for {printer.name} with "{template.name}". It is '
            'being sliced and will join the print queue when ready.',
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


TICKET_HISTORY = 25


@staff_required
def printers(request):
    """The printers as a grid, the slicer template library, and the "add a
    printer" form with how earlier requests went. Adding one files an
    AddPrinterTicket, which the print agent carries out."""
    if request.method == 'POST':
        form = AddPrinterTicketForm(request.POST)
        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.requested_by = request.user
            ticket.save()
            messages.success(
                request,
                f'Request to add "{ticket.name}" sent. The print agent will add it on its '
                'next check-in; this page shows when it is done.',
            )
            return redirect('printers')
    else:
        form = AddPrinterTicketForm()

    printer_tickets.reset_stale_claims()
    tickets = list(
        AddPrinterTicket.objects.select_related('requested_by', 'printer')[:TICKET_HISTORY]
    )
    return render(request, 'jobs/printers.html', {
        'form': form,
        'tickets': tickets,
        'printers': Printer.objects.select_related('default_template').prefetch_related('templates'),
        'templates': SlicerTemplate.objects.select_related('uploaded_by').prefetch_related('printers'),
        'via_agent': settings.BAMBUDDY_VIA_AGENT,
        'agents': list(PrinterAgent.objects.filter(is_active=True)),
        # Don't reload the page under someone who is filling the form in.
        'auto_refresh': request.method == 'GET' and any(ticket.is_open for ticket in tickets),
    })


@staff_required
@require_POST
def printer_ticket_cancel(request, pk):
    ticket = get_object_or_404(AddPrinterTicket, pk=pk)
    if printer_tickets.cancel(ticket):
        messages.success(request, f'Request to add "{ticket.name}" cancelled.')
    else:
        messages.error(
            request,
            f'The request to add "{ticket.name}" has already been picked up or finished.',
        )
    return redirect('printers')


@staff_required
def printer_detail(request, pk):
    """Manage one printer: rename it, take it out of use, and choose which
    slicer templates teachers can pick for it."""
    printer = get_object_or_404(Printer, pk=pk)
    form = PrinterForm(request.POST or None, instance=printer)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'{printer.name} saved.')
        return redirect('printers')
    return render(request, 'jobs/printer_detail.html', {
        # Re-read so a rejected rename doesn't show as the page heading.
        'printer': Printer.objects.get(pk=pk),
        'form': form,
        'has_templates': SlicerTemplate.objects.exists(),
    })


@staff_required
def template_upload(request):
    """Add a .3mf to the template library. `?printer=<id>` ticks that printer."""
    if request.method == 'POST':
        form = SlicerTemplateForm(request.POST, request.FILES, uploaded_by=request.user)
        if form.is_valid():
            template = form.save()
            messages.success(request, f'Template "{template.name}" uploaded.')
            return redirect('printers')
    else:
        form = SlicerTemplateForm(initial={'printers': request.GET.getlist('printer')})
    return render(request, 'jobs/template_upload.html', {'form': form})


@staff_required
def template_download(request, pk):
    template = get_object_or_404(SlicerTemplate, pk=pk)
    return FileResponse(
        template.file.open('rb'),
        as_attachment=True,
        filename=os.path.basename(template.file.name),
    )


@staff_required
@require_POST
def template_delete(request, pk):
    template = get_object_or_404(SlicerTemplate, pk=pk)
    if delete_template(template):
        messages.success(request, f'Template "{template.name}" deleted.')
    else:
        messages.error(
            request,
            f'Template "{template.name}" is in use by a job waiting to be sliced. '
            'Try again once it has been sliced.',
        )
    return redirect('printers')
