from django.contrib.auth import login
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect, render

from .forms import SignUpForm


class CmsLoginView(LoginView):
    template_name = 'accounts/login.html'


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
    return render(request, 'accounts/signup.html', {'form': form})
