from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from django.urls import reverse

from accounts.models import Classroom
from attachments.models import Attachment

from .models import Job
from .services import claim_next_job, reset_stale_claims, slice_claimed_job
from .slicing import SlicerNotConfigured, SlicingFailed

User = get_user_model()


class ReviewTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.student = User.objects.create_user('student', password='pw')
        cls.teacher = User.objects.create_user('teacher', password='pw', is_staff=True)
        cls.root = User.objects.create_superuser('root', password='pw')
        cls.attachment = Attachment.objects.create(
            user=cls.student,
            name='part',
            file=SimpleUploadedFile('part.stl', b'solid x'),
        )
        cls.classroom = Classroom.objects.create(name='Period 1')
        cls.classroom.teachers.add(cls.teacher)
        cls.classroom.students.add(cls.student)

    def make_job(self, **kwargs):
        kwargs.setdefault('classroom', self.classroom)
        return Job.objects.create(user=self.student, attachment=self.attachment, **kwargs)


class ReviewAccessTests(ReviewTestBase):
    def test_anonymous_redirected_to_login(self):
        response = self.client.get(reverse('job-review'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)

    def test_student_forbidden(self):
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse('job-review')).status_code, 403)
        job = self.make_job()
        response = self.client.post(reverse('job-review-reject', args=[job.pk]))
        self.assertEqual(response.status_code, 403)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.PENDING)

    def test_staff_allowed(self):
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get(reverse('job-review')).status_code, 200)

    def test_get_not_allowed_on_actions(self):
        self.client.force_login(self.teacher)
        job = self.make_job()
        response = self.client.get(reverse('job-review-reject', args=[job.pk]))
        self.assertEqual(response.status_code, 405)


class DjangoAdminAccessTests(ReviewTestBase):
    def test_staff_cannot_use_admin(self):
        self.client.force_login(self.teacher)
        response = self.client.get('/admin/jobs/job/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response.url)

    def test_superuser_can_use_admin(self):
        self.client.force_login(self.root)
        self.assertEqual(self.client.get('/admin/jobs/job/').status_code, 200)


class ReviewListTests(ReviewTestBase):
    def test_lists_all_statuses_newest_first(self):
        old = self.make_job(status=Job.Status.FINISHED)
        mid = self.make_job(status=Job.Status.REJECTED)
        new = self.make_job()
        self.client.force_login(self.teacher)
        response = self.client.get(reverse('job-review'))
        self.assertEqual(list(response.context['jobs']), [new, mid, old])
        self.assertEqual(response.context['pending_count'], 1)


class ReviewActionTests(ReviewTestBase):
    def setUp(self):
        self.client.force_login(self.teacher)

    @mock.patch('jobs.services.slice_to_gcode')
    def test_approve_returns_immediately_without_slicing(self, slice_mock):
        job = self.make_job()
        response = self.client.post(reverse('job-review-approve', args=[job.pk]))
        self.assertRedirects(response, reverse('job-review'))
        slice_mock.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.APPROVED)
        self.assertIsNotNone(job.approved_at)
        self.assertEqual(job.slicing_state, 'waiting')

    def test_retry_failed_job_clears_error(self):
        job = self.make_job(status=Job.Status.APPROVED, slicing_error='boom')
        self.client.post(reverse('job-review-approve', args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.slicing_error, '')
        self.assertEqual(job.slicing_state, 'waiting')

    def test_cannot_retry_job_being_sliced(self):
        job = self.make_job(
            status=Job.Status.APPROVED, slicing_started_at=timezone.now(), slicing_error='old',
        )
        self.client.post(reverse('job-review-approve', args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.slicing_error, 'old')

    def test_cannot_approve_rejected_or_finished(self):
        for status in (Job.Status.REJECTED, Job.Status.FINISHED):
            job = self.make_job(status=status)
            self.client.post(reverse('job-review-approve', args=[job.pk]))
            job.refresh_from_db()
            self.assertEqual(job.status, status)

    def test_reject_pending(self):
        job = self.make_job()
        self.client.post(reverse('job-review-reject', args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.REJECTED)

    def test_cannot_reject_queued(self):
        job = self.make_job(status=Job.Status.QUEUED)
        self.client.post(reverse('job-review-reject', args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)

    def test_page_shows_failure_and_retry(self):
        self.make_job(status=Job.Status.APPROVED, slicing_error='OrcaSlicer exited 1')
        response = self.client.get(reverse('job-review'))
        self.assertContains(response, 'Slicing failed')
        self.assertContains(response, 'OrcaSlicer exited 1')
        self.assertContains(response, 'Retry')
        self.assertNotContains(response, 'http-equiv="refresh"')

    def test_page_auto_refreshes_while_slicing(self):
        self.make_job(status=Job.Status.APPROVED)
        response = self.client.get(reverse('job-review'))
        self.assertContains(response, 'Waiting to slice')
        self.assertContains(response, 'http-equiv="refresh"')


class ExportClassTests(ReviewTestBase):
    def export(self, **data):
        self.client.force_login(self.student)
        return self.client.post(reverse('attachment-export', args=[self.attachment.pk]), data)

    def test_single_class_is_picked_automatically(self):
        self.export()
        self.assertEqual(Job.objects.get().classroom, self.classroom)

    def test_multiple_classes_require_a_choice(self):
        other = Classroom.objects.create(name='Period 2')
        other.students.add(self.student)
        self.export()
        self.assertFalse(Job.objects.exists())
        self.export(classroom=other.pk)
        self.assertEqual(Job.objects.get().classroom, other)

    def test_cannot_submit_to_class_student_is_not_in(self):
        Classroom.objects.create(name='Period 2').students.add(self.student)
        stranger = Classroom.objects.create(name='Not mine')
        self.export(classroom=stranger.pk)
        self.assertFalse(Job.objects.exists())

    def test_classless_student_exports_without_class(self):
        self.classroom.students.remove(self.student)
        self.export()
        self.assertIsNone(Job.objects.get().classroom)

    def test_picker_only_shown_for_multiple_classes(self):
        self.client.force_login(self.student)
        self.assertNotContains(self.client.get(reverse('attachment-list')), 'name="classroom"')
        Classroom.objects.create(name='Period 2').students.add(self.student)
        self.assertContains(self.client.get(reverse('attachment-list')), 'name="classroom"')


class ReviewScopeTests(ReviewTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other_teacher = User.objects.create_user('other', password='pw', is_staff=True)
        cls.other_class = Classroom.objects.create(name='Period 2')
        cls.other_class.teachers.add(cls.other_teacher)

    def review_jobs(self, user, **params):
        self.client.force_login(user)
        return list(self.client.get(reverse('job-review'), params).context['jobs'])

    def test_teacher_sees_only_own_classes(self):
        mine = self.make_job()
        theirs = self.make_job(classroom=self.other_class)
        classless = self.make_job(classroom=None)
        self.assertEqual(self.review_jobs(self.teacher), [mine])
        self.assertEqual(self.review_jobs(self.other_teacher), [theirs])
        self.assertEqual(self.review_jobs(self.root), [classless, theirs, mine])

    def test_teacher_sees_own_job(self):
        own_attachment = Attachment.objects.create(
            user=self.other_teacher, name='mine', file=SimpleUploadedFile('m.stl', b'x'),
        )
        job = Job.objects.create(user=self.other_teacher, attachment=own_attachment)
        self.assertEqual(self.review_jobs(self.other_teacher), [job])

    def test_cannot_act_on_other_class_job(self):
        job = self.make_job()
        self.client.force_login(self.other_teacher)
        for name in ('job-review-approve', 'job-review-reject'):
            self.assertEqual(self.client.post(reverse(name, args=[job.pk])).status_code, 404)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.PENDING)

    def test_class_filter(self):
        self.other_class.teachers.add(self.teacher)
        mine = self.make_job()
        theirs = self.make_job(classroom=self.other_class)
        self.assertEqual(self.review_jobs(self.teacher, classroom=self.classroom.pk), [mine])
        self.assertEqual(self.review_jobs(self.teacher, classroom=self.other_class.pk), [theirs])
        self.assertEqual(self.review_jobs(self.teacher), [theirs, mine])

    def test_filter_ignores_out_of_scope_class(self):
        mine = self.make_job()
        self.make_job(classroom=self.other_class)
        self.assertEqual(self.review_jobs(self.teacher, classroom=self.other_class.pk), [mine])
        self.assertEqual(self.review_jobs(self.teacher, classroom='none'), [mine])

    def test_superuser_no_class_filter(self):
        self.make_job()
        classless = self.make_job(classroom=None)
        self.assertEqual(self.review_jobs(self.root, classroom='none'), [classless])

    def test_action_keeps_filter(self):
        job = self.make_job()
        self.client.force_login(self.teacher)
        response = self.client.post(
            reverse('job-review-reject', args=[job.pk]), {'classroom': self.classroom.pk},
        )
        self.assertRedirects(response, f"{reverse('job-review')}?classroom={self.classroom.pk}")

    def test_staff_download_scoped(self):
        self.make_job()
        url = reverse('attachment-download', args=[self.attachment.pk])
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(self.other_teacher)
        self.assertEqual(self.client.get(url).status_code, 404)


class SliceWorkerTests(ReviewTestBase):
    def approved(self, **kwargs):
        return self.make_job(status=Job.Status.APPROVED, approved_at=timezone.now(), **kwargs)

    def test_claim_takes_oldest_waiting_job_once(self):
        first = self.approved()
        second = self.approved()
        self.assertEqual(claim_next_job(), first)
        self.assertEqual(claim_next_job(), second)
        self.assertIsNone(claim_next_job())

    def test_claim_skips_failed_pending_and_queued(self):
        self.approved(slicing_error='x')
        self.make_job()
        self.make_job(status=Job.Status.QUEUED)
        self.assertIsNone(claim_next_job())

    @mock.patch('jobs.services.slice_to_gcode')
    def test_success_clears_claim(self, slice_mock):
        job = self.approved()
        slice_claimed_job(claim_next_job())
        slice_mock.assert_called_once()
        job.refresh_from_db()
        self.assertIsNone(job.slicing_started_at)
        self.assertEqual(job.slicing_error, '')

    @mock.patch('jobs.services.slice_to_gcode', side_effect=SlicingFailed('boom'))
    def test_failure_records_error_and_is_not_retried(self, _slice):
        job = self.approved()
        self.assertFalse(slice_claimed_job(claim_next_job()))
        job.refresh_from_db()
        self.assertEqual(job.slicing_state, 'failed')
        self.assertEqual(job.slicing_error, 'boom')
        self.assertIsNone(claim_next_job())

    @mock.patch('jobs.services.slice_to_gcode', side_effect=SlicerNotConfigured('no slicer'))
    def test_not_configured_records_error(self, _slice):
        job = self.approved()
        slice_claimed_job(claim_next_job())
        job.refresh_from_db()
        self.assertEqual(job.slicing_error, 'no slicer')

    @mock.patch('jobs.services.slice_to_gcode', side_effect=RuntimeError('bug'))
    def test_unexpected_error_is_contained(self, _slice):
        job = self.approved()
        self.assertFalse(slice_claimed_job(claim_next_job()))
        job.refresh_from_db()
        self.assertEqual(job.slicing_state, 'failed')

    def test_stale_claim_released(self):
        stale = timezone.now() - timedelta(seconds=settings.ORCA_SLICER_TIMEOUT + 120)
        fresh = self.approved(slicing_started_at=timezone.now())
        old = self.approved(slicing_started_at=stale)
        self.assertEqual(reset_stale_claims(), 1)
        old.refresh_from_db()
        fresh.refresh_from_db()
        self.assertIsNone(old.slicing_started_at)
        self.assertIsNotNone(fresh.slicing_started_at)

    @mock.patch('jobs.services.slice_to_gcode')
    def test_command_once_drains_waiting_jobs(self, slice_mock):
        # The real slice_to_gcode moves the job to "queued"; mimic that so the
        # worker doesn't see the job as still waiting.
        slice_mock.side_effect = lambda job: Job.objects.filter(pk=job.pk).update(
            status=Job.Status.QUEUED,
        )
        self.approved()
        self.approved()
        call_command('slice_worker', '--once')
        self.assertEqual(slice_mock.call_count, 2)
