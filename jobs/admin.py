from django.contrib import admin, messages

from .models import (
    AddPrinterTicket,
    Filament,
    Job,
    Printer,
    PrinterAgent,
    PrinterFilament,
    PrinterModel,
)
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
        'dispatch_error', 'dispatched_at', 'agent', 'release_requested_at',
        'release_error', 'released_at', 'failed_at',
    )
    actions = [approve_jobs, reject_jobs]


@admin.register(PrinterModel)
class PrinterModelAdmin(admin.ModelAdmin):
    list_display = ('name', 'settings_path')
    search_fields = ('name',)


@admin.register(Filament)
class FilamentAdmin(admin.ModelAdmin):
    list_display = ('filament_type', 'color', 'preset_path')
    list_filter = ('filament_type',)
    search_fields = ('filament_type', 'color')


class PrinterFilamentInline(admin.TabularInline):
    model = PrinterFilament
    extra = 0
    max_num = Printer.MAX_FILAMENT_SLOTS


@admin.register(Printer)
class PrinterAdmin(admin.ModelAdmin):
    list_display = ('name', 'printer_model', 'bambuddy_printer_id', 'serial_number', 'is_active')
    list_filter = ('is_active', 'printer_model')
    search_fields = ('name', 'serial_number')
    inlines = [PrinterFilamentInline]


@admin.register(AddPrinterTicket)
class AddPrinterTicketAdmin(admin.ModelAdmin):
    """A record of who added which printer. Tickets are made on /printers/
    and changed only by the print agent, so this is read-only; the access
    code is never shown."""
    list_display = ('name', 'printer_model', 'status', 'requested_by', 'agent', 'created_at')
    list_filter = ('status',)
    search_fields = ('name', 'serial_number')
    exclude = ('access_code',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


def _show_key(modeladmin, request, agent, key):
    modeladmin.message_user(
        request,
        f'New key for "{agent.name}": {key} — copy it now into JOHNCAD_AGENT_KEY in '
        'the agent\'s config; it is stored hashed and won\'t be shown again.',
        messages.WARNING,
    )


@admin.action(description='Generate a new key (the old one stops working)')
def regenerate_keys(modeladmin, request, queryset):
    for agent in queryset:
        key = agent.set_new_key()
        agent.save(update_fields=['key_hash', 'key_hint'])
        _show_key(modeladmin, request, agent, key)


@admin.register(PrinterAgent)
class PrinterAgentAdmin(admin.ModelAdmin):
    list_display = ('name', 'key_hint', 'is_active', 'handles_unassigned', 'last_seen_at')
    list_filter = ('is_active',)
    filter_horizontal = ('printers',)
    readonly_fields = ('key_hint', 'created_at', 'last_seen_at')
    actions = [regenerate_keys]

    def save_model(self, request, obj, form, change):
        key = None if change else obj.set_new_key()
        super().save_model(request, obj, form, change)
        if key:
            _show_key(self, request, obj, key)
