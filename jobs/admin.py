from django.contrib import admin, messages
from django.utils import timezone

from .models import Job
from .slicing import SlicerNotConfigured, SlicingFailed, slice_to_gcode


@admin.action(description='Approve selected jobs')
def approve_jobs(modeladmin, request, queryset):
    pending = queryset.filter(status=Job.Status.PENDING)
    queued_count = 0
    not_configured_count = 0
    for job in pending:
        job.status = Job.Status.APPROVED
        job.approved_at = timezone.now()
        job.save(update_fields=['status', 'approved_at'])
        try:
            slice_to_gcode(job)
            queued_count += 1
        except SlicerNotConfigured:
            not_configured_count += 1
        except SlicingFailed as exc:
            modeladmin.message_user(
                request,
                f'Job #{job.id} approved but slicing failed, left parked in '
                f'"approved": {exc}',
                level=messages.ERROR,
            )
    if queued_count:
        modeladmin.message_user(request, f'{queued_count} job(s) sliced and queued.')
    if not_configured_count:
        modeladmin.message_user(
            request,
            f'{not_configured_count} job(s) approved but left parked in "approved" -- '
            'the slicer is not configured (binary not found).',
            level=messages.WARNING,
        )


@admin.action(description='Reject selected jobs')
def reject_jobs(modeladmin, request, queryset):
    updated = queryset.filter(status=Job.Status.PENDING).update(status=Job.Status.REJECTED)
    modeladmin.message_user(request, f'{updated} job(s) rejected.')


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ('id', 'attachment', 'user', 'status', 'created_at', 'approved_at')
    list_filter = ('status',)
    search_fields = ('attachment__name', 'user__username')
    readonly_fields = ('created_at', 'approved_at', 'working_at', 'finished_at')
    actions = [approve_jobs, reject_jobs]
