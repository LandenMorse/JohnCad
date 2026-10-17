from django.conf import settings
from django.db import models


def job_gcode_upload_path(instance, filename):
    return f'jobs/user_{instance.user_id}/{filename}'


class Job(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        QUEUED = 'queued', 'Queued'
        WORKING = 'working', 'Working'
        FINISHED = 'finished', 'Finished'
        REJECTED = 'rejected', 'Rejected'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='jobs',
    )
    attachment = models.ForeignKey(
        'attachments.Attachment',
        # Jobs are the print log: a file that has been exported can't be deleted
        # on its own, but deleting the whole user (who owns the jobs) still works.
        on_delete=models.RESTRICT,
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
    gcode = models.FileField(upload_to=job_gcode_upload_path, blank=True)
    # Slicing is done by the `slice_worker` command, not in the web request.
    # An "approved" job with no G-code is waiting for / being sliced / failed:
    # see slicing_state.
    slicing_started_at = models.DateTimeField(null=True, blank=True)
    slicing_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    working_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

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

    def __str__(self):
        return f'Job #{self.id} ({self.attachment.name}, {self.user.username})'
