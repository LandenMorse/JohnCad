from django.contrib import admin

from .models import Job, Printer
from .services import approve_job, reject_job


@admin.action(description='Approve selected jobs')
def approve_jobs(modeladmin, request, queryset):
    # "approved" jobs are included so ones whose slice failed can be retried by
    # re-running this action. Slicing itself is done by the slice_worker command.
    candidates = queryset.filter(status__in=[Job.Status.PENDING, Job.Status.APPROVED])
    approved = sum(approve_job(job) for job in candidates)
    message = f'{approved} job(s) approved; the slice worker will slice and queue them.'
    if approved < candidates.count():
        message += (
            ' Others were skipped: set a printer on the job first (or they '
            'can no longer be approved).'
        )
    modeladmin.message_user(request, message)


@admin.action(description='Reject selected jobs')
def reject_jobs(modeladmin, request, queryset):
    rejected = sum(reject_job(job) for job in queryset)
    modeladmin.message_user(request, f'{rejected} job(s) rejected.')


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ('id', 'attachment', 'user', 'printer', 'status', 'created_at', 'approved_at')
    list_filter = ('status', 'printer')
    search_fields = ('attachment__name', 'user__username')
    readonly_fields = (
        'created_at', 'approved_at', 'working_at', 'finished_at',
        'slicing_started_at', 'slicing_error',
        'bambuddy_file_id', 'bambuddy_queue_id', 'dispatch_started_at',
        'dispatch_error', 'dispatched_at', 'released_at', 'failed_at',
    )
    actions = [approve_jobs, reject_jobs]


@admin.register(Printer)
class PrinterAdmin(admin.ModelAdmin):
    list_display = ('name', 'bambuddy_printer_id', 'is_active', 'slicer_settings')
    list_filter = ('is_active',)
    search_fields = ('name',)
