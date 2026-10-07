import secrets
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.utils import timezone

SIGNUP_CODE_LIFETIME = timedelta(hours=24)
# No 0/O, 1/I/L: codes get read off a whiteboard and typed by hand.
SIGNUP_CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
SIGNUP_CODE_LENGTH = 8


class Classroom(models.Model):
    """A teacher's class ("Class" in the UI; `class` is a Python keyword)."""

    name = models.CharField(max_length=100)
    teachers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='taught_classes',
    )
    students = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='classes',
        blank=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'class'
        verbose_name_plural = 'classes'

    def __str__(self):
        return self.name

    def can_manage(self, user):
        return user.is_superuser or self.teachers.filter(pk=user.pk).exists()

    def active_codes(self):
        return self.codes.filter(expires_at__gt=timezone.now())


class SignupCode(models.Model):
    """Lets anyone holding it create an account in (or join) `classroom`.

    Multi-use until `expires_at`. Accounts created with it are unaffected by
    expiry; only the code stops working.
    """

    code = models.CharField(max_length=SIGNUP_CODE_LENGTH, unique=True)
    classroom = models.ForeignKey(Classroom, on_delete=models.CASCADE, related_name='codes')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.code

    @property
    def is_active(self):
        return self.expires_at > timezone.now()

    def revoke(self):
        self.expires_at = timezone.now()
        self.save(update_fields=['expires_at'])

    @staticmethod
    def normalize(raw):
        return ''.join((raw or '').split()).replace('-', '').upper()

    @classmethod
    def lookup(cls, raw):
        """The active code matching user input `raw`, or None."""
        code = cls.normalize(raw)
        if not code:
            return None
        return cls.objects.filter(code=code, expires_at__gt=timezone.now()).select_related(
            'classroom',
        ).first()

    @classmethod
    def generate(cls, classroom, created_by=None):
        for _ in range(10):
            code = ''.join(secrets.choice(SIGNUP_CODE_ALPHABET) for _ in range(SIGNUP_CODE_LENGTH))
            try:
                with transaction.atomic():
                    return cls.objects.create(
                        code=code,
                        classroom=classroom,
                        created_by=created_by,
                        expires_at=timezone.now() + SIGNUP_CODE_LIFETIME,
                    )
            except IntegrityError:
                continue
        raise RuntimeError('Could not generate a unique signup code.')
