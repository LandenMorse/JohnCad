from django.contrib.admin.apps import AdminConfig


class SuperuserAdminConfig(AdminConfig):
    """Use SuperuserAdminSite as admin.site (Django admin is superuser-only)."""
    default_site = 'cms.admin.SuperuserAdminSite'
