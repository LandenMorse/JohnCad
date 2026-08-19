from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.shortcuts import get_object_or_404, redirect, render

from .forms import AttachmentForm, SignUpForm
from .models import Attachment


class CmsLoginView(LoginView):
    template_name = 'attachments/login.html'


def signup(request):
    if request.user.is_authenticated:
        return redirect('attachment-list')
    if request.method == 'POST':
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect('attachment-list')
    else:
        form = SignUpForm()
    return render(request, 'attachments/signup.html', {'form': form})


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
    })


@login_required
def attachment_delete(request, pk):
    attachment = get_object_or_404(Attachment, pk=pk, user=request.user)
    if request.method == 'POST':
        attachment.file.delete(save=False)
        attachment.delete()
    return redirect('attachment-list')
