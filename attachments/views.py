import os

from django.contrib.auth.decorators import login_required
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render

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
        'jobs': Job.objects.filter(user=request.user).select_related('attachment'),
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
    attachment = get_object_or_404(Attachment, pk=pk, user=request.user)
    return FileResponse(
        attachment.file.open('rb'),
        as_attachment=True,
        filename=os.path.basename(attachment.file.name),
    )
