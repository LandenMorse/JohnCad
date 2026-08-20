from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

User = get_user_model()

# Re-register the default User admin so the admin panel shows Users too.
if admin.site.is_registered(User):
    admin.site.unregister(User)
admin.site.register(User, UserAdmin)
