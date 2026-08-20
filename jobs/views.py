from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from attachments.models import Attachment

from .models import Job


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
