from django.conf import settings
from django.db import models


def job_sliced_file_upload_path(instance, filename):
    return f'jobs/user_{instance.user_id}/{filename}'


# Old name, still referenced by migration 0001.
job_gcode_upload_path = job_sliced_file_upload_path


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
        reaches Bambuddy, then 'staged' (waiting for the plate to be cleared)
        and 'released'. Else ''."""
        if self.status != self.Status.QUEUED:
            return ''
        if self.dispatch_error:
            return 'failed'
        if not self.bambuddy_queue_id:
            return 'dispatching' if self.dispatch_started_at else 'waiting'
        return 'released' if self.released_at else 'staged'

    def __str__(self):
        return f'Job #{self.id} ({self.attachment.name}, {self.user.username})'
