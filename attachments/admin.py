from django.contrib import admin

from .models import Attachment


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'uploaded_at')
    list_filter = ('uploaded_at',)
    search_fields = ('name', 'user__username')
