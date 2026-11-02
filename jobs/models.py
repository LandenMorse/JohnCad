import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.db.models import Q


def job_sliced_file_upload_path(instance, filename):
    return f'jobs/user_{instance.user_id}/{filename}'


# Old name, still referenced by migration 0001.
job_gcode_upload_path = job_sliced_file_upload_path


class Printer(models.Model):
    """A physical printer a teacher can send a job to.

    Registered in Bambuddy first (IP, serial, access code live there); this row
    only links JohnCad's name for it to Bambuddy's printer id and says which
    OrcaSlicer presets slice for it, since a sliced file only prints on the
    printer model it was sliced for.
    """
    name = models.CharField(max_length=100, unique=True)
    bambuddy_printer_id = models.PositiveIntegerField(
        help_text="Bambuddy's id for this printer (GET /api/v1/printers/ on Bambuddy).",
    )
    slicer_settings = models.CharField(
        max_length=1000,
        blank=True,
        help_text=(
            'Semicolon-separated machine + process preset .json paths for this '
            'printer (OrcaSlicer --load-settings). Blank = use ORCA_SLICER_SETTINGS.'
        ),
    )
    slicer_filaments = models.CharField(
        max_length=1000,
        blank=True,
        help_text='Semicolon-separated filament preset .json path(s). Optional.',
    )
    is_active = models.BooleanField(
        default=True,
        help_text='Untick to stop offering this printer to teachers (keeps job history).',
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Job(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        QUEUED = 'queued', 'Queued'
        WORKING = 'working', 'Working'
        FINISHED = 'finished', 'Finished'
        REJECTED = 'rejected', 'Rejected'
        FAILED = 'failed', 'Failed'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='jobs',
    )
    attachment = models.ForeignKey(
        'attachments.Attachment',
        on_delete=models.CASCADE,
        related_name='jobs',
    )
    # The class the Student submitted this job to; decides which teachers
    # review it. Null for students in no class (superusers review those) and
    # if the class is later deleted -- print history is kept either way.
    classroom = models.ForeignKey(
        'accounts.Classroom',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='jobs',
    )
    # Chosen by the teacher when approving; decides the slicer presets and which
    # Bambuddy printer the job goes to. Null for jobs made before printers
    # existed, or when no printers are set up (then the .env settings apply).
    printer = models.ForeignKey(
        Printer,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='jobs',
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    # The sliced .gcode.3mf that gets uploaded to Bambuddy.
    sliced_file = models.FileField(upload_to=job_sliced_file_upload_path, blank=True)
    # Slicing is done by the `slice_worker` command, not in the web request.
    # An "approved" job with no sliced file is waiting for / being sliced /
    # failed: see slicing_state.
    slicing_started_at = models.DateTimeField(null=True, blank=True)
    slicing_error = models.TextField(blank=True)
    # Dispatch to Bambuddy is done by the `dispatch_worker` command. A "queued"
    # job with no bambuddy_queue_id is waiting for / being dispatched / failed:
    # see dispatch_state. The file id is kept so a retry doesn't re-upload.
    bambuddy_file_id = models.CharField(max_length=64, blank=True)
    bambuddy_queue_id = models.CharField(max_length=64, blank=True, db_index=True)
    dispatch_started_at = models.DateTimeField(null=True, blank=True)
    dispatch_error = models.TextField(blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    # The print agent that claimed/dispatched this job (BAMBUDDY_VIA_AGENT).
    agent = models.ForeignKey(
        'PrinterAgent',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='handled_jobs',
    )
    # With a print agent, "plate cleared, release next" only records the
    # request; the agent starts the print and confirms (released_at) or
    # reports why it couldn't (release_error, e.g. spool too short).
    release_requested_at = models.DateTimeField(null=True, blank=True)
    release_error = models.TextField(blank=True)
    # Set when staff click "plate cleared, release next" and Bambuddy starts it.
    released_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    working_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def slicing_state(self):
        """'waiting' / 'slicing' / 'failed' for an approved job, else ''."""
        if self.status != self.Status.APPROVED:
            return ''
        if self.slicing_error:
            return 'failed'
        if self.slicing_started_at:
            return 'slicing'
        return 'waiting'

    @property
    def dispatch_state(self):
        """For a queued job: 'waiting' / 'dispatching' / 'failed' before it
        reaches Bambuddy, then 'staged' (waiting for the plate to be cleared),
        'releasing' (release asked of the print agent) and 'released'.
        Else ''."""
        if self.status != self.Status.QUEUED:
            return ''
        if self.dispatch_error:
            return 'failed'
        if not self.bambuddy_queue_id:
            return 'dispatching' if self.dispatch_started_at else 'waiting'
        if self.released_at:
            return 'released'
        return 'releasing' if self.release_requested_at else 'staged'

    def __str__(self):
        return f'Job #{self.id} ({self.attachment.name}, {self.user.username})'


class PrinterAgent(models.Model):
    """A print agent: the small program on the school Pi that pulls approved
    jobs from this site over HTTPS and hands them to its local Bambuddy (see
    agent/README.md). Only used with BAMBUDDY_VIA_AGENT=True.

    It authenticates with an API key. Only a hash of the key is stored; the
    key itself is shown once, when it's generated in /admin/ or by
    `manage.py create_agent_key`.
    """
    KEY_PREFIX = 'jca_'

    name = models.CharField(max_length=100, unique=True)
    printers = models.ManyToManyField(
        Printer,
        blank=True,
        related_name='agents',
        help_text='Printers this agent prints for. Leave empty for all printers.',
    )
    handles_unassigned = models.BooleanField(
        default=True,
        help_text=(
            'Also take jobs with no printer chosen (they go to the agent\'s '
            'BAMBUDDY_PRINTER_ID).'
        ),
    )
    is_active = models.BooleanField(
        default=True,
        help_text='Untick to block this agent (its key stops working).',
    )
    key_hash = models.CharField(max_length=64, unique=True, editable=False)
    key_hint = models.CharField(
        max_length=16, editable=False,
        help_text='Start of the key, to tell keys apart.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @staticmethod
    def hash_key(key):
        return hashlib.sha256(key.encode()).hexdigest()

    def set_new_key(self):
        """Give the agent a new random key (replacing any old one) and return
        it. The caller must save() and show the key; it can't be recovered."""
        key = self.KEY_PREFIX + secrets.token_urlsafe(32)
        self.key_hash = self.hash_key(key)
        self.key_hint = key[:len(self.KEY_PREFIX) + 6]
        return key

    @classmethod
    def for_key(cls, key):
        """The active agent with this key, or None."""
        if not key or not key.startswith(cls.KEY_PREFIX):
            return None
        return cls.objects.filter(key_hash=cls.hash_key(key), is_active=True).first()

    def jobs_in_scope(self):
        """Jobs this agent may see and act on."""
        printer_ids = list(self.printers.values_list('pk', flat=True))
        if not printer_ids:
            scope = Q(printer__isnull=False)
        else:
            scope = Q(printer_id__in=printer_ids)
        if self.handles_unassigned:
            scope |= Q(printer__isnull=True)
        return Job.objects.filter(scope)
