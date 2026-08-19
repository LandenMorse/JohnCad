from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

from .models import Attachment

User = get_user_model()


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'uploaded_at', 'ready_to_print', 'fetched_at')
    list_editable = ('ready_to_print',)
    list_filter = ('ready_to_print', 'uploaded_at')
    search_fields = ('name', 'user__username')
    readonly_fields = ('fetched_at',)


# Re-register the default User admin so the admin panel shows Users too.
if admin.site.is_registered(User):
    admin.site.unregister(User)
admin.site.register(User, UserAdmin)
