from django.contrib.auth.views import LoginView


class CmsLoginView(LoginView):
    template_name = 'accounts/login.html'
