from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import Attachment

User = get_user_model()


class AttachmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user('owner', password='pw')
        cls.other = User.objects.create_user('other', password='pw')
        cls.attachment = Attachment.objects.create(
            user=cls.owner,
            name='part',
            file=SimpleUploadedFile('part.stl', b'solid x'),
        )

    def test_login_required(self):
        response = self.client.get(reverse('attachment-list'))
        self.assertRedirects(response, '/login/?next=/')

    def test_upload_stl(self):
        self.client.force_login(self.other)
        response = self.client.post(reverse('attachment-list'), {
            'name': 'gear',
            'file': SimpleUploadedFile('gear.stl', b'solid gear'),
        })
        self.assertRedirects(response, reverse('attachment-list'))
        self.assertTrue(Attachment.objects.filter(user=self.other, name='gear').exists())

    def test_upload_rejects_other_file_types(self):
        self.client.force_login(self.other)
        response = self.client.post(reverse('attachment-list'), {
            'name': 'notes',
            'file': SimpleUploadedFile('notes.txt', b'hello'),
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Attachment.objects.filter(user=self.other).exists())

    def test_list_shows_only_own_files(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse('attachment-list'))
        self.assertEqual(list(response.context['attachments']), [])

    def test_owner_can_download(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('attachment-download', args=[self.attachment.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), b'solid x')

    def test_other_student_cannot_download(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse('attachment-download', args=[self.attachment.pk]))
        self.assertEqual(response.status_code, 404)

    def test_other_student_cannot_delete(self):
        self.client.force_login(self.other)
        response = self.client.post(reverse('attachment-delete', args=[self.attachment.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Attachment.objects.filter(pk=self.attachment.pk).exists())

    def test_owner_delete_removes_row_and_file(self):
        attachment = Attachment.objects.create(
            user=self.owner,
            name='scrap',
            file=SimpleUploadedFile('scrap.stl', b'solid scrap'),
        )
        storage, name = attachment.file.storage, attachment.file.name
        self.client.force_login(self.owner)
        self.client.post(reverse('attachment-delete', args=[attachment.pk]))
        self.assertFalse(Attachment.objects.filter(pk=attachment.pk).exists())
        self.assertFalse(storage.exists(name))
