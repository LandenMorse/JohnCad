from django.contrib.admin import AdminSite


class SuperuserAdminSite(AdminSite):
    """Django admin for developers only.

    Teachers are `is_staff` (which the stock admin accepts) and approve jobs on
    the /review/ page instead, so the admin additionally requires superuser.
    """

    def has_permission(self, request):
        return request.user.is_active and request.user.is_superuser
