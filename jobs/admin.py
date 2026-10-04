from django.contrib import admin

from .models import Job
from .services import approve_job, reject_job


@admin.action(description='Approve selected jobs')
def approve_jobs(modeladmin, request, queryset):
    # "approved" jobs are included so ones whose slice failed can be retried by
    # re-running this action. Slicing itself is done by the slice_worker command.
    candidates = queryset.filter(status__in=[Job.Status.PENDING, Job.Status.APPROVED])
    approved = sum(approve_job(job) for job in candidates)
    modeladmin.message_user(
        request,
        f'{approved} job(s) approved; the slice worker will slice and queue them.',
    )


@admin.action(description='Reject selected jobs')
def reject_jobs(modeladmin, request, queryset):
    rejected = sum(reject_job(job) for job in queryset)
    modeladmin.message_user(request, f'{rejected} job(s) rejected.')


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ('id', 'attachment', 'user', 'status', 'created_at', 'approved_at')
    list_filter = ('status',)
    search_fields = ('attachment__name', 'user__username')
    readonly_fields = (
        'created_at', 'approved_at', 'working_at', 'finished_at',
        'slicing_started_at', 'slicing_error',
    )
    actions = [approve_jobs, reject_jobs]
