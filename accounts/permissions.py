from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from .models import Classroom


def staff_required(view):
    """Staff (teachers) only: anonymous -> login page, non-staff -> 403."""
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def classes_visible_to(user):
    """Classes `user` can manage and review: all for superusers, else taught ones."""
    if user.is_superuser:
        return Classroom.objects.all()
    return user.taught_classes.all()
