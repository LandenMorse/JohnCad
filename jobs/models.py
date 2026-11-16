import hashlib
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, RegexValidator
from django.db import models
from django.db.models import Q

from .threemf import Presets, read_presets, validate_slicer_template


def job_sliced_file_upload_path(instance, filename):
    return f'jobs/user_{instance.user_id}/{filename}'


# Old name, still referenced by migration 0001.
job_gcode_upload_path = job_sliced_file_upload_path


class SlicerTemplate(models.Model):
    """An OrcaSlicer project (.3mf) that jobs are sliced into.

    The project holds the machine, process and filament settings, so a
    template is everything the slicer needs to know about how to print: the
    uploaded .stl is added to it and arranged on its plate. Teachers upload
    templates on /printers/ and say which printers each one is for; the
    teacher approving a job picks one of the printer's templates.

    The file is never replaced once uploaded (upload a new template instead),
    so the presets read from it at upload stay true.
    """
    name = models.CharField(
        max_length=100, unique=True,
        help_text='What teachers see when choosing, e.g. "X1C PLA 0.2mm".',
    )
    file = models.FileField(
        'project file',
        upload_to='slicer_templates/',
        validators=[FileExtensionValidator(allowed_extensions=['3mf']), validate_slicer_template],
        help_text=(
            'An OrcaSlicer project saved with File > Save Project As. Anything '
            'left on its plate is printed with every job, so save it with an '
            'empty plate.'
        ),
    )
    # Read from the file when it is uploaded; shown to teachers so they can
    # tell templates apart.
    machine_preset = models.CharField(max_length=255, blank=True, editable=False)
    process_preset = models.CharField(max_length=255, blank=True, editable=False)
    filament_presets = models.JSONField(default=list, blank=True, editable=False)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name='slicer_templates',
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self._state.adding and self.file:
            try:
                presets = read_presets(self.file)
            except ValidationError:
                # Validation is the field validator's job (forms run it);
                # a file that can't be read just has no presets to show.
                presets = Presets()
            self.machine_preset = presets.machine
            self.process_preset = presets.process
            self.filament_presets = list(presets.filaments)
        super().save(*args, **kwargs)


class PrinterQuerySet(models.QuerySet):
    def approvable(self):
        """Printers a job can be approved for: in use, with a template."""
        return self.filter(is_active=True, templates__isnull=False).distinct()


class Printer(models.Model):
    """A physical printer a teacher can send a job to.

    Its IP address and access code live in Bambuddy, not here; this row links
    JohnCad's name for the printer to Bambuddy's printer id, and says which
    slicer templates can be used on it. Teachers add printers on /printers/
    (an AddPrinterTicket, carried out by the print agent) and manage them
    there.
    """
    name = models.CharField(max_length=100, unique=True)
    bambuddy_printer_id = models.PositiveIntegerField(
        help_text="Bambuddy's id for this printer (GET /api/v1/printers/ on Bambuddy).",
    )
    serial_number = models.CharField(max_length=32, blank=True)
    templates = models.ManyToManyField(
        SlicerTemplate,
        blank=True,
        related_name='printers',
        help_text='The slicer templates teachers can choose for this printer.',
    )
    default_template = models.ForeignKey(
        SlicerTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='default_for',
        help_text='The template offered first when approving a job for this printer.',
    )
    is_active = models.BooleanField(
        'in use',
        default=True,
        help_text='Untick to stop offering this printer to teachers (keeps job history).',
    )

    objects = PrinterQuerySet.as_manager()

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def offer_template(self, template):
        """Let teachers choose `template` for this printer; it becomes the
        default if the printer has none."""
        self.templates.add(template)
        if self.default_template_id is None:
            self.default_template = template
            self.save(update_fields=['default_template'])

    def set_templates(self, templates, default=None):
        """Replace the templates offered for this printer. `default` is kept
        only if it is one of them; otherwise the first one is the default."""
        templates = list(templates)
        self.templates.set(templates)
        self.default_template = default if default in templates else next(iter(templates), None)
        self.save(update_fields=['default_template'])


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
    # Both chosen by the teacher when approving. The printer decides which
    # Bambuddy printer the job goes to; the template (one of that printer's)
    # is what the job is sliced into. Null until approved, and on jobs from
    # before printers or templates existed.
    printer = models.ForeignKey(
        Printer,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='jobs',
    )
    # SET_NULL: a template can be deleted once nothing is waiting to be sliced
    # with it (services.delete_template); the print history stays.
    template = models.ForeignKey(
        SlicerTemplate,
        on_delete=models.SET_NULL,
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


class AddPrinterTicket(models.Model):
    """A teacher's request to add a printer (made on /printers/).

    The site can't reach the printers' network, so the print agent carries it
    out: it adds the printer to Bambuddy, waits for Bambuddy to connect to it,
    and reports Bambuddy's id for it. The site then creates the Printer and
    links it here. Logic: jobs/printer_tickets.py.

    The access code is only kept while the ticket is open; it is blanked as
    soon as the ticket succeeds, fails or is cancelled.
    """
    class Status(models.TextChoices):
        PENDING = 'pending', 'Waiting for the print agent'
        ADDING = 'adding', 'Adding to Bambuddy'
        SUCCEEDED = 'succeeded', 'Added'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'

    OPEN_STATUSES = (Status.PENDING, Status.ADDING)

    name = models.CharField(
        'printer name', max_length=100,
        help_text='What teachers will see when choosing a printer.',
    )
    ip_address = models.GenericIPAddressField(
        'IP address', protocol='IPv4',
        help_text="The printer's address on the school network (printer screen: Settings > Network).",
    )
    access_code = models.CharField(
        max_length=16, blank=True,
        validators=[RegexValidator(r'^[A-Za-z0-9]{8}$', 'The access code is 8 letters and digits.')],
        help_text="The 8-character LAN access code on the printer's screen.",
    )
    serial_number = models.CharField(
        max_length=32,
        validators=[RegexValidator(r'^[A-Z0-9]{10,32}$', 'The serial number is letters and digits only.')],
        help_text="On the printer's screen (Settings > Device) or the label on its back.",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    error = models.TextField(blank=True)
    bambuddy_printer_id = models.PositiveIntegerField(null=True, blank=True)
    # The printer this ticket created, once it succeeds.
    printer = models.OneToOneField(
        Printer, on_delete=models.SET_NULL, null=True, blank=True, related_name='ticket',
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='printer_tickets',
    )
    agent = models.ForeignKey(
        'PrinterAgent', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='printer_tickets',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Add printer "{self.name}" ({self.get_status_display()})'

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES
