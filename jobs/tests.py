import json
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from django.urls import reverse

from accounts.models import Classroom
from attachments.models import Attachment

from . import bambuddy
from .bambuddy import BambuddyError
from .dispatch import (
    claim_next_dispatch,
    dispatch_claimed_job,
    reset_stale_dispatch_claims,
    retry_dispatch,
    sync_from_bambuddy,
)
from .models import Job, Printer
from .services import approve_job, claim_next_job, reset_stale_claims, slice_claimed_job
from .slicing import SlicerNotConfigured, SlicingFailed, slice_to_3mf

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

    @mock.patch('jobs.services.slice_to_3mf')
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

    @mock.patch('jobs.services.slice_to_3mf')
    def test_success_clears_claim(self, slice_mock):
        job = self.approved()
        slice_claimed_job(claim_next_job())
        slice_mock.assert_called_once()
        job.refresh_from_db()
        self.assertIsNone(job.slicing_started_at)
        self.assertEqual(job.slicing_error, '')

    @mock.patch('jobs.services.slice_to_3mf', side_effect=SlicingFailed('boom'))
    def test_failure_records_error_and_is_not_retried(self, _slice):
        job = self.approved()
        self.assertFalse(slice_claimed_job(claim_next_job()))
        job.refresh_from_db()
        self.assertEqual(job.slicing_state, 'failed')
        self.assertEqual(job.slicing_error, 'boom')
        self.assertIsNone(claim_next_job())

    @mock.patch('jobs.services.slice_to_3mf', side_effect=SlicerNotConfigured('no slicer'))
    def test_not_configured_records_error(self, _slice):
        job = self.approved()
        slice_claimed_job(claim_next_job())
        job.refresh_from_db()
        self.assertEqual(job.slicing_error, 'no slicer')

    @mock.patch('jobs.services.slice_to_3mf', side_effect=RuntimeError('bug'))
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

    @mock.patch('jobs.services.slice_to_3mf')
    def test_command_once_drains_waiting_jobs(self, slice_mock):
        # The real slice_to_3mf moves the job to "queued"; mimic that so the
        # worker doesn't see the job as still waiting.
        slice_mock.side_effect = lambda job: Job.objects.filter(pk=job.pk).update(
            status=Job.Status.QUEUED,
        )
        self.approved()
        self.approved()
        call_command('slice_worker', '--once')
        self.assertEqual(slice_mock.call_count, 2)


BAMBUDDY_SETTINGS = dict(
    BAMBUDDY_URL='http://bambuddy.test',
    BAMBUDDY_API_KEY='key',
    BAMBUDDY_PRINTER_ID='1',
    BAMBUDDY_WEBHOOK_SECRET='s3cret',
    BAMBUDDY_STAGED=True,
)


@override_settings(**BAMBUDDY_SETTINGS)
class DispatchTestBase(ReviewTestBase):
    def sliced(self, **kwargs):
        kwargs.setdefault('status', Job.Status.QUEUED)
        kwargs.setdefault('approved_at', timezone.now())
        job = self.make_job(**kwargs)
        job.sliced_file.save(f'{job.id}.gcode.3mf', ContentFile(b'3mf'), save=True)
        return job

    def dispatched(self, **kwargs):
        kwargs.setdefault('bambuddy_file_id', '7')
        kwargs.setdefault('bambuddy_queue_id', '70')
        kwargs.setdefault('dispatched_at', timezone.now())
        return self.sliced(**kwargs)


class DispatchWorkerTests(DispatchTestBase):
    def test_claim_takes_oldest_undispatched_sliced_job_once(self):
        first = self.sliced()
        second = self.sliced()
        self.dispatched()
        self.sliced(dispatch_error='x')
        self.make_job(status=Job.Status.QUEUED)  # not sliced
        self.assertEqual(claim_next_dispatch(), first)
        self.assertEqual(claim_next_dispatch(), second)
        self.assertIsNone(claim_next_dispatch())

    @mock.patch('jobs.bambuddy.enqueue', return_value='55')
    @mock.patch('jobs.bambuddy.upload_file', return_value='9')
    def test_success_uploads_enqueues_and_stores_ids(self, upload, enqueue):
        job = self.sliced()
        self.assertTrue(dispatch_claimed_job(claim_next_dispatch()))
        self.assertEqual(upload.call_args.args[1], f'johncad-job-{job.id}.gcode.3mf')
        enqueue.assert_called_once_with('9', None)
        job.refresh_from_db()
        self.assertEqual((job.bambuddy_file_id, job.bambuddy_queue_id), ('9', '55'))
        self.assertIsNotNone(job.dispatched_at)
        self.assertIsNone(job.dispatch_started_at)
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertEqual(job.dispatch_state, 'staged')

    @mock.patch('jobs.bambuddy.enqueue', side_effect=BambuddyError('queue down'))
    @mock.patch('jobs.bambuddy.upload_file', return_value='9')
    def test_failure_records_error_keeps_upload_and_is_not_retried(self, upload, _enqueue):
        job = self.sliced()
        self.assertFalse(dispatch_claimed_job(claim_next_dispatch()))
        job.refresh_from_db()
        self.assertEqual(job.dispatch_state, 'failed')
        self.assertEqual(job.dispatch_error, 'queue down')
        self.assertEqual(job.bambuddy_file_id, '9')
        self.assertIsNone(claim_next_dispatch())

        # Retry re-enqueues without uploading again.
        self.assertTrue(retry_dispatch(job))
        with mock.patch('jobs.bambuddy.enqueue', return_value='56'):
            self.assertTrue(dispatch_claimed_job(claim_next_dispatch()))
        self.assertEqual(upload.call_count, 1)

    @override_settings(BAMBUDDY_URL='')
    def test_not_configured_records_error(self):
        job = self.sliced()
        dispatch_claimed_job(claim_next_dispatch())
        job.refresh_from_db()
        self.assertIn('BAMBUDDY_URL', job.dispatch_error)

    def test_stale_claim_released(self):
        stale = timezone.now() - timedelta(seconds=settings.BAMBUDDY_TIMEOUT * 3 + 120)
        old = self.sliced(dispatch_started_at=stale)
        fresh = self.sliced(dispatch_started_at=timezone.now())
        self.assertEqual(reset_stale_dispatch_claims(), 1)
        old.refresh_from_db()
        fresh.refresh_from_db()
        self.assertIsNone(old.dispatch_started_at)
        self.assertIsNotNone(fresh.dispatch_started_at)

    @mock.patch('jobs.bambuddy.queue_item_event', return_value=None)
    @mock.patch('jobs.bambuddy.enqueue', side_effect=['1', '2'])
    @mock.patch('jobs.bambuddy.upload_file', side_effect=['a', 'b'])
    def test_command_once_drains_and_polls(self, _upload, enqueue, poll):
        self.sliced()
        self.sliced()
        call_command('dispatch_worker', '--once')
        self.assertEqual(enqueue.call_count, 2)
        self.assertEqual(sorted(c.args[0] for c in poll.call_args_list), ['1', '2'])

    @mock.patch('jobs.bambuddy.requests.request')
    def test_client_sends_key_and_queue_body(self, request):
        request.return_value = mock.Mock(status_code=200, content=b'{"id": 3}')
        request.return_value.json.return_value = {'id': 3}
        self.assertEqual(bambuddy.enqueue('9'), '3')
        method, url = request.call_args.args
        self.assertEqual((method, url), ('POST', 'http://bambuddy.test/api/v1/queue/'))
        self.assertEqual(request.call_args.kwargs['headers'], {'X-API-Key': 'key'})
        self.assertEqual(
            request.call_args.kwargs['json'],
            {'library_file_id': 9, 'printer_id': 1, 'manual_start': True},
        )

    @override_settings(BAMBUDDY_STAGED=False)
    @mock.patch('jobs.bambuddy.requests.request')
    def test_client_unstaged_queue_body(self, request):
        request.return_value = mock.Mock(status_code=200, content=b'{"id": 3}')
        request.return_value.json.return_value = {'id': 3}
        bambuddy.enqueue('9')
        self.assertIs(request.call_args.kwargs['json']['manual_start'], False)

    @mock.patch('jobs.bambuddy.requests.request')
    def test_client_upload(self, request):
        request.return_value = mock.Mock(status_code=200, content=b'{"id": 12}')
        request.return_value.json.return_value = {'id': 12, 'filename': 'x'}
        self.assertEqual(bambuddy.upload_file(b'3mf', 'johncad-job-1.gcode.3mf'), '12')
        method, url = request.call_args.args
        self.assertEqual((method, url), ('POST', 'http://bambuddy.test/api/v1/library/files/'))
        self.assertEqual(
            request.call_args.kwargs['files'], {'file': ('johncad-job-1.gcode.3mf', b'3mf')},
        )

    def test_default_url_is_localhost(self):
        from cms import settings as project_settings
        self.assertEqual(project_settings.BAMBUDDY_URL, 'http://127.0.0.1:8080')


class WebhookTests(DispatchTestBase):
    url = '/api/bambuddy/webhook/'

    def post(self, event, job=None, secret='s3cret', **variables):
        # Bambuddy's "generic" webhook payload: flat, with the print's
        # subtask name (upload name minus .gcode.3mf) as "filename".
        if job is not None:
            variables.setdefault('filename', f'johncad-job-{job.id}')
        payload = {
            'title': 'Print', 'message': '...', 'timestamp': '2026-10-29T03:00:00',
            'source': 'Bambuddy', 'event': event, 'printer': 'X1C', **variables,
        }
        return self.client.post(
            self.url,
            json.dumps(payload),
            content_type='application/json',
            headers={'Authorization': f'Bearer {secret}'} if secret else {},
        )

    def test_secret_required(self):
        job = self.dispatched()
        self.assertEqual(self.post('print_start', job, secret='nope').status_code, 403)
        self.assertEqual(self.post('print_start', job, secret='').status_code, 403)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)

    def test_secret_in_query_string(self):
        job = self.dispatched()
        response = self.client.post(
            self.url + '?secret=s3cret',
            json.dumps({'event': 'print_start', 'filename': f'johncad-job-{job.id}'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)

    @override_settings(BAMBUDDY_WEBHOOK_SECRET='')
    def test_blank_secret_rejects_everything(self):
        job = self.dispatched()
        self.assertEqual(self.post('print_start', job, secret='').status_code, 403)

    def test_full_lifecycle_and_idempotency(self):
        job = self.dispatched()
        self.assertEqual(self.post('print_start', job).json()['status'], 'ok')
        self.assertEqual(self.post('print_start', job).json()['status'], 'unchanged')
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.WORKING)
        self.assertIsNotNone(job.working_at)
        self.post('print_complete', job)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.FINISHED)
        self.assertIsNotNone(job.finished_at)
        self.assertEqual(self.post('print_complete', job).json()['status'], 'unchanged')

    def test_complete_before_start_is_409(self):
        job = self.dispatched()
        self.assertEqual(self.post('print_complete', job).status_code, 409)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)

    def test_failed_and_stopped(self):
        job = self.dispatched(status=Job.Status.WORKING)
        self.post('print_failed', job, reason='Spaghetti detected')
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn('print_failed: Spaghetti detected', job.dispatch_error)
        other = self.dispatched(status=Job.Status.WORKING, bambuddy_file_id='8')
        self.post('print_stopped', other, reason='Unknown')
        other.refresh_from_db()
        self.assertEqual(other.status, Job.Status.FAILED)
        self.assertEqual(other.dispatch_error, 'Bambuddy reported print_stopped.')

    def test_full_filename_also_matches(self):
        job = self.dispatched()
        self.post('print_start', filename=f'johncad-job-{job.id}.gcode.3mf')
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.WORKING)

    def test_similar_name_does_not_match(self):
        job = self.dispatched()
        self.post('print_start', filename=f'johncad-job-{job.id}0')
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)

    def test_unknown_events_and_jobs_ignored(self):
        job = self.dispatched()
        self.assertEqual(self.post('print_progress', job).json()['status'], 'ignored')
        self.assertEqual(self.post('print_start', filename='benchy').json()['status'], 'ignored')
        self.assertEqual(self.post('test').json()['status'], 'ignored')
        undispatched = self.sliced()
        self.assertEqual(self.post('print_start', undispatched).json()['status'], 'ignored')

    def test_bad_json(self):
        response = self.client.post(
            self.url, 'nope', content_type='application/json',
            headers={'Authorization': 'Bearer s3cret'},
        )
        self.assertEqual(response.status_code, 400)

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_old_printer_controller_api_is_gone(self):
        self.client.force_login(self.root)
        for path in ('/api/jobs/queue/', '/api/jobs/1/gcode/'):
            self.assertEqual(self.client.get(path).status_code, 404)
        for path in ('/api/jobs/1/working/', '/api/jobs/1/finished/'):
            self.assertEqual(self.client.post(path).status_code, 404)


class PollingFallbackTests(DispatchTestBase):
    def test_sync_applies_remote_status(self):
        started = self.dispatched(bambuddy_queue_id='1')
        done = self.dispatched(bambuddy_queue_id='2')  # missed its start webhook
        failed = self.dispatched(bambuddy_queue_id='3', status=Job.Status.WORKING)
        untouched = self.dispatched(bambuddy_queue_id='4')
        events = {'1': 'started', '2': 'completed', '3': 'failed', '4': None}
        with mock.patch('jobs.bambuddy.queue_item_event', side_effect=events.get):
            self.assertEqual(sync_from_bambuddy(), 3)
        statuses = [Job.objects.get(pk=j.pk).status for j in (started, done, failed, untouched)]
        self.assertEqual(
            statuses,
            [Job.Status.WORKING, Job.Status.FINISHED, Job.Status.FAILED, Job.Status.QUEUED],
        )

    @mock.patch('jobs.bambuddy.queue_item_event', return_value='missing')
    def test_missing_item_left_alone(self, _poll):
        job = self.dispatched()
        self.assertEqual(sync_from_bambuddy(), 0)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)

    @mock.patch('jobs.bambuddy.queue_item_event')
    def test_no_poll_when_nothing_in_flight(self, poll):
        self.sliced()
        self.assertEqual(sync_from_bambuddy(), 0)
        poll.assert_not_called()

    @mock.patch('jobs.bambuddy.requests.request')
    def test_queue_item_event_maps_statuses(self, request):
        expected = {
            'pending': None, 'printing': 'started', 'completed': 'completed',
            'failed': 'failed', 'skipped': 'failed', 'cancelled': 'failed',
        }
        for status, event in expected.items():
            request.return_value = mock.Mock(status_code=200, content=b'{}')
            request.return_value.json.return_value = {'id': 5, 'status': status}
            self.assertEqual(bambuddy.queue_item_event('5'), event, status)
        self.assertEqual(request.call_args.args, ('GET', 'http://bambuddy.test/api/v1/queue/5'))
        request.return_value = mock.Mock(status_code=404, content=b'{}', text='{"detail": "not found"}')
        self.assertEqual(bambuddy.queue_item_event('5'), 'missing')


class DispatchButtonTests(DispatchTestBase):
    def test_review_retry_dispatch_error(self):
        job = self.sliced(dispatch_error='down')
        self.client.force_login(self.teacher)
        response = self.client.get(reverse('job-review'))
        self.assertContains(response, 'Sending failed')
        self.assertContains(response, reverse('job-review-retry-dispatch', args=[job.pk]))
        self.client.post(reverse('job-review-retry-dispatch', args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.dispatch_state, 'waiting')

    def test_retry_failed_print_requeues(self):
        job = self.dispatched(
            status=Job.Status.FAILED, released_at=timezone.now(), failed_at=timezone.now(),
            dispatch_error='print_failed',
        )
        self.client.force_login(self.teacher)
        self.client.post(reverse('job-review-retry-dispatch', args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertEqual(job.bambuddy_queue_id, '')
        self.assertEqual(job.bambuddy_file_id, '7')
        self.assertEqual(claim_next_dispatch(), job)

    def test_cannot_retry_healthy_or_other_class_job(self):
        job = self.dispatched()
        self.client.force_login(self.teacher)
        self.client.post(reverse('job-review-retry-dispatch', args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.bambuddy_queue_id, '70')
        other_teacher = User.objects.create_user('other', password='pw', is_staff=True)
        self.client.force_login(other_teacher)
        failed = self.sliced(dispatch_error='x')
        response = self.client.post(reverse('job-review-retry-dispatch', args=[failed.pk]))
        self.assertEqual(response.status_code, 404)

    @mock.patch('jobs.bambuddy.start_item')
    def test_release_next_releases_oldest_staged(self, release):
        first = self.dispatched(dispatched_at=timezone.now() - timedelta(minutes=5), bambuddy_queue_id='81')
        second = self.dispatched()
        self.client.force_login(self.teacher)
        self.assertContains(self.client.get(reverse('queue')), 'Plate cleared, release next')
        self.client.post(reverse('queue-release-next'))
        release.assert_called_once_with('81')
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNotNone(first.released_at)
        self.assertIsNone(second.released_at)
        self.assertEqual(first.dispatch_state, 'released')

    @mock.patch('jobs.bambuddy.start_item', side_effect=BambuddyError('offline'))
    def test_release_next_error_shown_and_not_marked(self, _release):
        job = self.dispatched()
        self.client.force_login(self.teacher)
        response = self.client.post(reverse('queue-release-next'), follow=True)
        self.assertContains(response, 'offline')
        job.refresh_from_db()
        self.assertIsNone(job.released_at)

    def test_release_next_staff_only(self):
        self.dispatched()
        self.client.force_login(self.student)
        self.assertNotContains(self.client.get(reverse('queue')), 'release next')
        self.assertEqual(self.client.post(reverse('queue-release-next')).status_code, 403)


class SlicingOutputTests(ReviewTestBase):
    @override_settings(ORCA_SLICER_SETTINGS='m.json;p.json', ORCA_SLICER_CMD='orca')
    def test_exports_3mf_and_queues(self):
        job = self.make_job(status=Job.Status.APPROVED)

        def fake_run(command, **kwargs):
            self.assertIn('--export-3mf', command)
            outdir = Path(command[command.index('--outputdir') + 1])
            (outdir / command[command.index('--export-3mf') + 1]).write_bytes(b'3mf')
            return mock.Mock(returncode=0, stdout='', stderr='')

        with mock.patch('jobs.slicing.subprocess.run', side_effect=fake_run):
            slice_to_3mf(job)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertTrue(job.sliced_file.name.endswith('.gcode.3mf'))

    @override_settings(ORCA_SLICER_SETTINGS='m.json;p.json', ORCA_SLICER_CMD='orca')
    def test_no_3mf_output_fails(self):
        job = self.make_job(status=Job.Status.APPROVED)
        result = mock.Mock(returncode=0, stdout='', stderr='')
        with mock.patch('jobs.slicing.subprocess.run', return_value=result):
            with self.assertRaises(SlicingFailed):
                slice_to_3mf(job)


def fake_orca(outputs):
    """subprocess.run stand-in that records the command and writes a .3mf."""
    def run(command, **kwargs):
        outputs.append(command)
        outdir = Path(command[command.index('--outputdir') + 1])
        (outdir / command[command.index('--export-3mf') + 1]).write_bytes(b'3mf')
        return mock.Mock(returncode=0, stdout='', stderr='')
    return run


class PrinterSelectionTests(ReviewTestBase):
    """Teacher picks the printer on /review/; it is required once printers exist."""

    def setUp(self):
        self.p1 = Printer.objects.create(name='X1C', bambuddy_printer_id=4)
        self.p2 = Printer.objects.create(name='P1S', bambuddy_printer_id=9)
        self.client.force_login(self.teacher)

    def approve(self, job, **data):
        return self.client.post(reverse('job-review-approve', args=[job.pk]), data, follow=True)

    def test_approve_with_printer_sets_it(self):
        job = self.make_job()
        response = self.approve(job, printer=self.p2.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.APPROVED)
        self.assertEqual(job.printer, self.p2)
        self.assertContains(response, 'approved for P1S')

    def test_approve_without_printer_is_refused(self):
        job = self.make_job()
        response = self.approve(job)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.PENDING)
        self.assertContains(response, 'Choose a printer')

    def test_unknown_or_inactive_printer_is_refused(self):
        self.p1.is_active = False
        self.p1.save()
        job = self.make_job()
        for value in (self.p1.pk, 99999, 'abc'):
            self.approve(job, printer=value)
            job.refresh_from_db()
            self.assertEqual(job.status, Job.Status.PENDING)
            self.assertIsNone(job.printer)

    def test_without_any_printers_set_up_approval_still_works(self):
        Printer.objects.all().delete()
        job = self.make_job()
        self.approve(job)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.APPROVED)
        self.assertIsNone(job.printer)

    def test_slicing_retry_can_change_printer(self):
        job = self.make_job(
            status=Job.Status.APPROVED, slicing_error='bad preset', printer=self.p1,
        )
        self.approve(job, printer=self.p2.pk)
        job.refresh_from_db()
        self.assertEqual(job.printer, self.p2)
        self.assertEqual(job.slicing_error, '')

    def test_admin_style_approve_needs_a_printer(self):
        job = self.make_job()
        self.assertFalse(approve_job(job))
        job.printer = self.p1
        job.save()
        self.assertTrue(approve_job(job))

    def test_review_page_shows_picker_and_printer_column(self):
        self.make_job()
        self.make_job(status=Job.Status.APPROVED, printer=self.p1)
        response = self.client.get(reverse('job-review'))
        self.assertContains(response, '<option value="%d">X1C</option>' % self.p1.pk, html=False)
        self.assertContains(response, 'name="printer"')
        self.assertContains(response, '<td>X1C</td>', html=False)

    def test_inactive_printer_not_offered(self):
        self.p2.is_active = False
        self.p2.save()
        self.make_job()
        self.assertNotContains(self.client.get(reverse('job-review')), 'P1S')

    def test_no_picker_when_no_printers(self):
        Printer.objects.all().delete()
        self.make_job()
        self.assertNotContains(self.client.get(reverse('job-review')), 'name="printer"')

    def test_printer_with_jobs_cannot_be_deleted(self):
        from django.db.models import ProtectedError
        self.make_job(printer=self.p1)
        with self.assertRaises(ProtectedError):
            self.p1.delete()


@override_settings(ORCA_SLICER_CMD='orca')
class PrinterSlicingTests(ReviewTestBase):
    def slice(self, job):
        commands = []
        with mock.patch('jobs.slicing.subprocess.run', side_effect=fake_orca(commands)):
            slice_to_3mf(job)
        return commands[0]

    @override_settings(ORCA_SLICER_SETTINGS='env-m.json;env-p.json', ORCA_SLICER_FILAMENTS='env-f.json')
    def test_printer_presets_replace_env_presets(self):
        printer = Printer.objects.create(
            name='X1C', bambuddy_printer_id=1,
            slicer_settings='x1c-m.json;x1c-p.json', slicer_filaments='x1c-f.json',
        )
        command = self.slice(self.make_job(status=Job.Status.APPROVED, printer=printer))
        self.assertEqual(command[command.index('--load-settings') + 1], 'x1c-m.json;x1c-p.json')
        self.assertEqual(command[command.index('--load-filaments') + 1], 'x1c-f.json')

    @override_settings(ORCA_SLICER_SETTINGS='env-m.json;env-p.json', ORCA_SLICER_FILAMENTS='env-f.json')
    def test_printer_presets_never_mix_with_env_filaments(self):
        printer = Printer.objects.create(
            name='P1S', bambuddy_printer_id=2, slicer_settings='p1s-m.json;p1s-p.json',
        )
        command = self.slice(self.make_job(status=Job.Status.APPROVED, printer=printer))
        self.assertEqual(command[command.index('--load-settings') + 1], 'p1s-m.json;p1s-p.json')
        self.assertNotIn('--load-filaments', command)

    @override_settings(ORCA_SLICER_SETTINGS='env-m.json;env-p.json', ORCA_SLICER_FILAMENTS='env-f.json')
    def test_printer_without_presets_falls_back_to_env_pair(self):
        printer = Printer.objects.create(name='A1', bambuddy_printer_id=3)
        command = self.slice(self.make_job(status=Job.Status.APPROVED, printer=printer))
        self.assertEqual(command[command.index('--load-settings') + 1], 'env-m.json;env-p.json')
        self.assertEqual(command[command.index('--load-filaments') + 1], 'env-f.json')

    @override_settings(ORCA_SLICER_SETTINGS='')
    def test_no_presets_anywhere_names_the_printer(self):
        printer = Printer.objects.create(name='A1', bambuddy_printer_id=3)
        job = self.make_job(status=Job.Status.APPROVED, printer=printer)
        with self.assertRaisesMessage(SlicerNotConfigured, 'printer "A1"'):
            slice_to_3mf(job)

    @override_settings(ORCA_SLICER_SETTINGS='env-m.json;env-p.json')
    def test_job_without_printer_uses_env(self):
        command = self.slice(self.make_job(status=Job.Status.APPROVED))
        self.assertEqual(command[command.index('--load-settings') + 1], 'env-m.json;env-p.json')


class PrinterDispatchTests(DispatchTestBase):
    def setUp(self):
        self.p1 = Printer.objects.create(name='X1C', bambuddy_printer_id=4)
        self.p2 = Printer.objects.create(name='P1S', bambuddy_printer_id=9)

    @mock.patch('jobs.bambuddy.enqueue', return_value='55')
    @mock.patch('jobs.bambuddy.upload_file', return_value='9')
    def test_dispatch_uses_the_jobs_bambuddy_printer(self, _upload, enqueue):
        self.sliced(printer=self.p2)
        dispatch_claimed_job(claim_next_dispatch())
        enqueue.assert_called_once_with('9', 9)

    @mock.patch('jobs.bambuddy.requests.request')
    def test_enqueue_body_uses_given_printer_id(self, request):
        request.return_value = mock.Mock(status_code=200, content=b'{"id": 3}')
        request.return_value.json.return_value = {'id': 3}
        bambuddy.enqueue('9', 4)
        self.assertEqual(
            request.call_args.kwargs['json'],
            {'library_file_id': 9, 'printer_id': 4, 'manual_start': True},
        )

    @mock.patch('jobs.bambuddy.start_item')
    def test_release_is_per_printer(self, release):
        a = self.dispatched(printer=self.p1, bambuddy_queue_id='81')
        b = self.dispatched(printer=self.p2, bambuddy_queue_id='82')
        self.client.force_login(self.teacher)
        page = self.client.get(reverse('queue'))
        self.assertContains(page, 'Plate cleared on X1C, release next')
        self.assertContains(page, 'Plate cleared on P1S, release next')
        self.client.post(reverse('queue-release-next'), {'printer': self.p2.pk})
        release.assert_called_once_with('82')
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertIsNone(a.released_at)
        self.assertIsNotNone(b.released_at)

    @mock.patch('jobs.bambuddy.start_item')
    def test_release_picks_oldest_staged_on_that_printer_only(self, release):
        self.dispatched(printer=self.p1, bambuddy_queue_id='90',
                        dispatched_at=timezone.now() - timedelta(minutes=9))
        self.dispatched(printer=self.p2, bambuddy_queue_id='91',
                        dispatched_at=timezone.now() - timedelta(minutes=5))
        self.dispatched(printer=self.p2, bambuddy_queue_id='92')
        self.client.force_login(self.teacher)
        self.client.post(reverse('queue-release-next'), {'printer': self.p2.pk})
        release.assert_called_once_with('91')

    @mock.patch('jobs.bambuddy.start_item')
    def test_jobs_without_printer_still_release_with_no_printer_field(self, release):
        self.dispatched(bambuddy_queue_id='70')
        self.client.force_login(self.teacher)
        self.assertContains(self.client.get(reverse('queue')), 'Plate cleared, release next')
        self.client.post(reverse('queue-release-next'))
        release.assert_called_once_with('70')

    def test_release_unknown_printer_is_an_error(self):
        self.client.force_login(self.teacher)
        response = self.client.post(reverse('queue-release-next'), {'printer': '999'}, follow=True)
        self.assertContains(response, 'no longer exists')

    def test_queue_page_shows_printer_column(self):
        self.dispatched(printer=self.p1)
        self.client.force_login(self.student)
        self.assertContains(self.client.get(reverse('queue')), '<td>X1C</td>', html=False)
