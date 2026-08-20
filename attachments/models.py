from django.conf import settings
from django.core.validators import FileExtensionValidator
from django.db import models


def attachment_upload_path(instance, filename):
    return f'attachments/user_{instance.user_id}/{filename}'


class Attachment(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='attachments',
    )
    name = models.CharField(max_length=255)
    file = models.FileField(
        upload_to=attachment_upload_path,
        validators=[FileExtensionValidator(allowed_extensions=['stl'])],
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f'{self.name} ({self.user})'
