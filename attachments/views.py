import os

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render

from accounts.permissions import classes_visible_to
from jobs.models import Job

from .forms import AttachmentForm
from .models import Attachment


@login_required
def attachment_upload(request):
    if request.method == 'POST':
        form = AttachmentForm(request.POST, request.FILES)
        if form.is_valid():
            attachment = form.save(commit=False)
            attachment.user = request.user
            attachment.save()
            return redirect('attachment-list')
    else:
        form = AttachmentForm()
    return render(request, 'attachments/attachment_list.html', {
        'form': form,
        'attachments': Attachment.objects.filter(user=request.user),
        'jobs': Job.objects.filter(user=request.user).select_related('attachment', 'classroom'),
        'classes': list(request.user.classes.all()),
    })


@login_required
def attachment_delete(request, pk):
    attachment = get_object_or_404(Attachment, pk=pk, user=request.user)
    if request.method == 'POST':
        attachment.file.delete(save=False)
        attachment.delete()
    return redirect('attachment-list')


@login_required
def attachment_download(request, pk):
    user = request.user
    if user.is_superuser:
        attachments = Attachment.objects.all()
    elif user.is_staff:
        # Teachers: their own files, plus files submitted to a class they teach.
        attachments = Attachment.objects.filter(
            Q(user=user) | Q(jobs__classroom__in=classes_visible_to(user)),
        ).distinct()
    else:
        attachments = Attachment.objects.filter(user=user)
    attachment = get_object_or_404(attachments, pk=pk)
    return FileResponse(
        attachment.file.open('rb'),
        as_attachment=True,
        filename=os.path.basename(attachment.file.name),
    )
