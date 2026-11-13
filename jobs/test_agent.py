"""Print agent: the /agent-api/ endpoints and the agent itself
(agent/johncad_agent.py), run against the Django test client and a fake
Bambuddy."""
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from .bambuddy_client import BambuddyError
from .models import AddPrinterTicket, Job, Printer, PrinterAgent, PrinterModel
from .tests import ReviewTestBase

if sys.platform == 'win32':
    # The agent locks its state dir with fcntl, which only exists on Unix
    # (it runs on a Linux box next to Bambuddy).
    raise unittest.SkipTest('the print agent is Unix-only (fcntl)')

sys.path.insert(0, str(Path(settings.BASE_DIR) / 'agent'))
import johncad_agent  # noqa: E402

PAYLOAD = bytes(range(256)) * 41 + b'tail'   # 10500 bytes, not a chunk multiple
API = '/agent-api/v1/'


class AgentTestBase(ReviewTestBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.mkdtemp()
        cls._override = override_settings(
            MEDIA_ROOT=cls._media, BAMBUDDY_VIA_AGENT=True, AGENT_POLL_INTERVAL=15,
            BAMBUDDY_STAGED=True,
        )
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.x1c = Printer.objects.create(name='X1C', bambuddy_printer_id=4)
        self.p1s = Printer.objects.create(name='P1S', bambuddy_printer_id=9)
        self.agent = PrinterAgent(name='school-pi')
        self.key = self.agent.set_new_key()
        self.agent.save()

    def sliced(self, payload=PAYLOAD, **kwargs):
        kwargs.setdefault('status', Job.Status.QUEUED)
        kwargs.setdefault('approved_at', timezone.now())
        kwargs.setdefault('printer', self.x1c)
        job = self.make_job(**kwargs)
        job.sliced_file.save(f'{job.id}.gcode.3mf', ContentFile(payload), save=True)
        return job

    def dispatched(self, **kwargs):
        kwargs.setdefault('bambuddy_file_id', '7')
        kwargs.setdefault('bambuddy_queue_id', '70')
        kwargs.setdefault('dispatched_at', timezone.now())
        kwargs.setdefault('agent', self.agent)
        return self.sliced(**kwargs)

    def api(self, method, path, body=None, key=None, **headers):
        key = self.key if key is None else key
        if key:
            headers['HTTP_AUTHORIZATION'] = f'Bearer {key}'
        if method == 'get':
            return self.client.get(API + path, **headers)
        return self.client.post(
            API + path, data=json.dumps(body or {}), content_type='application/json', **headers,
        )


class AgentKeyTests(AgentTestBase):
    def test_key_is_stored_hashed_and_looked_up(self):
        self.assertTrue(self.key.startswith('jca_'))
        self.assertNotIn(self.key, self.agent.key_hash)
        self.assertEqual(self.agent.key_hash, hashlib.sha256(self.key.encode()).hexdigest())
        self.assertEqual(PrinterAgent.for_key(self.key), self.agent)
        self.assertIsNone(PrinterAgent.for_key(self.key + 'x'))
        self.assertIsNone(PrinterAgent.for_key(''))

    def test_bad_missing_or_inactive_key_is_401(self):
        self.assertEqual(self.api('get', 'poll/', key='').status_code, 401)
        self.assertEqual(self.api('get', 'poll/', key='jca_nope').status_code, 401)
        PrinterAgent.objects.filter(pk=self.agent.pk).update(is_active=False)
        self.assertEqual(self.api('get', 'poll/').status_code, 401)

    def test_session_login_is_not_enough(self):
        self.client.force_login(self.root)
        self.assertEqual(self.api('get', 'poll/', key='').status_code, 401)

    @override_settings(BAMBUDDY_VIA_AGENT=False)
    def test_api_is_404_when_agent_mode_is_off(self):
        self.assertEqual(self.api('get', 'poll/').status_code, 404)

    def test_poll_records_last_seen(self):
        self.api('get', 'poll/')
        self.agent.refresh_from_db()
        self.assertIsNotNone(self.agent.last_seen_at)

    def test_create_agent_key_command(self):
        out = io.StringIO()
        call_command('create_agent_key', 'lab-pi', stdout=out)
        key = out.getvalue().strip().splitlines()[-1]
        self.assertEqual(PrinterAgent.for_key(key).name, 'lab-pi')
        call_command('create_agent_key', 'lab-pi', '--rotate', stdout=out)
        self.assertIsNone(PrinterAgent.for_key(key))

    def test_admin_add_shows_key_once(self):
        self.client.force_login(self.root)
        response = self.client.post(
            reverse('admin:jobs_printeragent_add'),
            {'name': 'new-pi', 'handles_unassigned': 'on', 'is_active': 'on'},
            follow=True,
        )
        agent = PrinterAgent.objects.get(name='new-pi')
        shown = [str(m) for m in response.context['messages']]
        self.assertEqual(len(shown), 2)    # the key + Django's "added" message
        key = next(m for m in shown if 'jca_' in m).split('"new-pi": ')[1].split(' ')[0]
        self.assertEqual(PrinterAgent.for_key(key), agent)


class AgentPollTests(AgentTestBase):
    def test_poll_lists_dispatch_release_and_track(self):
        waiting = self.sliced()
        staged = self.dispatched(release_requested_at=timezone.now(), bambuddy_queue_id='71')
        printing = self.dispatched(status=Job.Status.WORKING, bambuddy_queue_id='72')
        self.sliced(dispatch_error='down')          # needs a teacher Retry first
        self.make_job(status=Job.Status.APPROVED)   # not sliced yet
        data = self.api('get', 'poll/').json()
        self.assertEqual([j['id'] for j in data['dispatch']], [waiting.id])
        self.assertEqual(data['dispatch'][0]['printer_id'], 4)
        self.assertEqual(data['release'], [{'id': staged.id, 'bambuddy_queue_id': '71'}])
        self.assertEqual(
            sorted(j['id'] for j in data['track']), sorted([staged.id, printing.id]),
        )
        self.assertEqual(data['poll_interval'], 15)
        self.assertIs(data['staged'], True)

    def test_agent_only_sees_its_printers(self):
        mine = self.sliced(printer=self.x1c)
        self.sliced(printer=self.p1s)
        unassigned = self.sliced(printer=None)
        self.agent.printers.add(self.x1c)
        ids = [j['id'] for j in self.api('get', 'poll/').json()['dispatch']]
        self.assertEqual(ids, [mine.id, unassigned.id])
        PrinterAgent.objects.filter(pk=self.agent.pk).update(handles_unassigned=False)
        ids = [j['id'] for j in self.api('get', 'poll/').json()['dispatch']]
        self.assertEqual(ids, [mine.id])

    def test_other_agents_claims_are_hidden_but_go_stale(self):
        other = PrinterAgent(name='other')
        other.set_new_key()
        other.save()
        job = self.sliced(agent=other, dispatch_started_at=timezone.now())
        self.assertEqual(self.api('get', 'poll/').json()['dispatch'], [])
        Job.objects.filter(pk=job.pk).update(
            dispatch_started_at=timezone.now() - timedelta(seconds=settings.AGENT_CLAIM_TIMEOUT + 5),
        )
        self.assertEqual([j['id'] for j in self.api('get', 'poll/').json()['dispatch']], [job.id])


class AgentDispatchApiTests(AgentTestBase):
    def test_claim_returns_file_details(self):
        job = self.sliced()
        data = self.api('post', f'jobs/{job.id}/claim/').json()
        self.assertEqual(data['size'], len(PAYLOAD))
        self.assertEqual(data['sha256'], hashlib.sha256(PAYLOAD).hexdigest())
        self.assertEqual(data['file_name'], f'johncad-job-{job.id}.gcode.3mf')
        self.assertEqual(data['printer_id'], 4)
        job.refresh_from_db()
        self.assertEqual(job.agent, self.agent)
        self.assertEqual(job.dispatch_state, 'dispatching')

    def test_claim_out_of_scope_or_unavailable(self):
        self.agent.printers.add(self.x1c)
        other_printer = self.sliced(printer=self.p1s)
        self.assertEqual(self.api('post', f'jobs/{other_printer.id}/claim/').status_code, 404)
        done = self.dispatched()
        self.assertEqual(self.api('post', f'jobs/{done.id}/claim/').status_code, 409)

    def test_download_needs_claim_and_supports_ranges(self):
        job = self.sliced()
        self.assertEqual(self.api('get', f'jobs/{job.id}/file/').status_code, 409)
        self.api('post', f'jobs/{job.id}/claim/')
        whole = self.api('get', f'jobs/{job.id}/file/')
        self.assertEqual(whole.status_code, 200)
        self.assertEqual(b''.join(whole.streaming_content), PAYLOAD)
        part = self.api('get', f'jobs/{job.id}/file/', HTTP_RANGE='bytes=100-199')
        self.assertEqual(part.status_code, 206)
        self.assertEqual(part['Content-Range'], f'bytes 100-199/{len(PAYLOAD)}')
        self.assertEqual(part['Content-Length'], '100')
        self.assertEqual(b''.join(part.streaming_content), PAYLOAD[100:200])
        tail = self.api('get', f'jobs/{job.id}/file/', HTTP_RANGE='bytes=10000-')
        self.assertEqual(b''.join(tail.streaming_content), PAYLOAD[10000:])
        suffix = self.api('get', f'jobs/{job.id}/file/', HTTP_RANGE='bytes=-4')
        self.assertEqual(b''.join(suffix.streaming_content), b'tail')
        past_end = self.api('get', f'jobs/{job.id}/file/', HTTP_RANGE=f'bytes={len(PAYLOAD)}-')
        self.assertEqual(past_end.status_code, 416)
        self.assertEqual(past_end['Content-Range'], f'bytes */{len(PAYLOAD)}')
        multi = self.api('get', f'jobs/{job.id}/file/', HTTP_RANGE='bytes=0-1,5-6')
        self.assertEqual(multi.status_code, 416)

    def test_download_refreshes_the_claim(self):
        job = self.sliced()
        self.api('post', f'jobs/{job.id}/claim/')
        old = timezone.now() - timedelta(seconds=600)
        Job.objects.filter(pk=job.pk).update(dispatch_started_at=old)
        self.api('get', f'jobs/{job.id}/file/', HTTP_RANGE='bytes=0-9')
        job.refresh_from_db()
        self.assertGreater(job.dispatch_started_at, old)

    def test_dispatched_report_is_idempotent(self):
        job = self.sliced()
        self.api('post', f'jobs/{job.id}/claim/')
        body = {'bambuddy_file_id': '12', 'bambuddy_queue_id': '34'}
        self.assertEqual(self.api('post', f'jobs/{job.id}/dispatched/', body).json()['status'], 'ok')
        job.refresh_from_db()
        self.assertEqual((job.bambuddy_file_id, job.bambuddy_queue_id), ('12', '34'))
        self.assertIsNone(job.dispatch_started_at)
        self.assertEqual(job.dispatch_state, 'staged')
        self.assertEqual(
            self.api('post', f'jobs/{job.id}/dispatched/', body).json()['status'], 'unchanged',
        )
        other = {'bambuddy_file_id': '12', 'bambuddy_queue_id': '99'}
        self.assertEqual(self.api('post', f'jobs/{job.id}/dispatched/', other).status_code, 409)
        self.assertEqual(self.api('post', f'jobs/{job.id}/dispatched/', {}).status_code, 400)

    def test_dispatch_failed_shows_on_review_and_retry_reoffers(self):
        job = self.sliced()
        self.api('post', f'jobs/{job.id}/claim/')
        self.api('post', f'jobs/{job.id}/dispatch-failed/',
                 {'error': 'Bambuddy offline', 'bambuddy_file_id': '12'})
        job.refresh_from_db()
        self.assertEqual(job.dispatch_state, 'failed')
        self.assertEqual(job.bambuddy_file_id, '12')
        self.client.force_login(self.teacher)
        self.assertContains(self.client.get(reverse('job-review')), 'Bambuddy offline')
        self.client.post(reverse('job-review-retry-dispatch', args=[job.pk]))
        self.client.logout()
        dispatch = self.api('get', 'poll/').json()['dispatch']
        self.assertEqual(dispatch[0]['bambuddy_file_id'], '12')

    def test_bad_json_is_400(self):
        job = self.sliced()
        response = self.client.post(
            API + f'jobs/{job.id}/dispatch-failed/', data='nope',
            content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {self.key}',
        )
        self.assertEqual(response.status_code, 400)


class AgentReleaseTests(AgentTestBase):
    def test_release_button_queues_request_for_agent(self):
        job = self.dispatched()
        self.client.force_login(self.teacher)
        page = self.client.get(reverse('queue'))
        self.assertContains(page, 'Plate cleared on X1C, release next')
        self.assertContains(page, 'has never checked in')
        with mock.patch('jobs.bambuddy.start_item') as start:
            response = self.client.post(
                reverse('queue-release-next'), {'printer': self.x1c.pk}, follow=True,
            )
        start.assert_not_called()
        self.assertContains(response, 'will start it on its next check-in')
        self.assertContains(response, 'waiting for the print agent')
        self.assertNotContains(response, 'Plate cleared on X1C, release next')
        job.refresh_from_db()
        self.assertEqual(job.dispatch_state, 'releasing')
        first_request = job.release_requested_at
        # A second click doesn't move on to the next job or re-stamp.
        self.client.post(reverse('queue-release-next'), {'printer': self.x1c.pk})
        job.refresh_from_db()
        self.assertEqual(job.release_requested_at, first_request)

    def test_released_and_release_failed_reports(self):
        job = self.dispatched(release_requested_at=timezone.now())
        self.assertEqual(self.api('post', f'jobs/{job.id}/release-failed/',
                                  {'error': 'Spool too short'}).status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.dispatch_state, 'staged')
        self.assertEqual(job.release_error, 'Spool too short')
        self.client.force_login(self.teacher)
        page = self.client.get(reverse('queue'))
        self.assertContains(page, 'Last release failed: Spool too short')
        self.assertContains(page, 'Plate cleared on X1C, release next')
        self.client.post(reverse('queue-release-next'), {'printer': self.x1c.pk})
        self.assertEqual(self.api('post', f'jobs/{job.id}/released/').status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.dispatch_state, 'released')
        self.assertEqual(job.release_error, '')
        self.assertEqual(self.api('post', f'jobs/{job.id}/released/').status_code, 409)


class AgentEventTests(AgentTestBase):
    def test_events_move_job_along(self):
        job = self.dispatched()
        self.assertEqual(self.api('post', f'jobs/{job.id}/event/', {'event': 'started'}).json()['status'], 'ok')
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.WORKING)
        self.api('post', f'jobs/{job.id}/event/', {'event': 'completed'})
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.FINISHED)

    def test_completed_while_queued_skips_missed_start(self):
        job = self.dispatched()
        self.api('post', f'jobs/{job.id}/event/', {'event': 'completed'})
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.FINISHED)
        self.assertIsNotNone(job.working_at)

    def test_failed_event_records_detail_and_retry_requeues(self):
        job = self.dispatched(release_requested_at=timezone.now(), released_at=timezone.now())
        self.api('post', f'jobs/{job.id}/event/', {'event': 'failed', 'detail': 'Nozzle clog'})
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertEqual(job.dispatch_error, 'Nozzle clog')
        self.client.force_login(self.teacher)
        self.client.post(reverse('job-review-retry-dispatch', args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertIsNone(job.release_requested_at)
        self.assertEqual(job.bambuddy_queue_id, '')
        self.assertEqual(job.bambuddy_file_id, '7')   # reused, no re-upload

    def test_invalid_events(self):
        job = self.dispatched(status=Job.Status.FINISHED)
        self.assertEqual(self.api('post', f'jobs/{job.id}/event/', {'event': 'started'}).status_code, 409)
        self.assertEqual(self.api('post', f'jobs/{job.id}/event/', {'event': 'exploded'}).status_code, 400)
        undispatched = self.sliced()
        self.assertEqual(
            self.api('post', f'jobs/{undispatched.id}/event/', {'event': 'started'}).status_code, 409,
        )


class AgentModeWorkerTests(AgentTestBase):
    def test_dispatch_worker_does_nothing_in_agent_mode(self):
        self.sliced()
        with mock.patch('jobs.bambuddy.upload_file') as upload:
            call_command('dispatch_worker', '--once')
        upload.assert_not_called()


# --- the agent program itself ------------------------------------------------

class TestClientSession:
    """Lets the agent's Home talk to the Django test client instead of HTTP."""

    def __init__(self, client):
        self.client = client
        self.headers = {}
        self.calls = []
        self.fail_next = 0     # simulate dropped connections

    def request(self, method, url, json=None, headers=None, timeout=None):
        path = url.split('://', 1)[1].split('/', 1)[1]
        path = '/' + path
        self.calls.append((method, path, (headers or {}).get('Range')))
        if self.fail_next:
            self.fail_next -= 1
            raise johncad_agent.requests.ConnectionError('connection reset by peer')
        extra = {'HTTP_' + k.upper().replace('-', '_'): v
                 for k, v in {**self.headers, **(headers or {})}.items()}
        if method == 'GET':
            response = self.client.get(path, **extra)
        else:
            response = self.client.generic(
                method, path, data=__import__('json').dumps(json or {}),
                content_type='application/json', **extra,
            )
        return FakeResponse(response, url)


class FakeResponse:
    def __init__(self, response, url):
        self.status_code = response.status_code
        self.headers = {k: v for k, v in response.items()}
        self.content = (b''.join(response.streaming_content)
                        if response.streaming else response.content)
        self.url = url

    @property
    def text(self):
        return self.content.decode(errors='replace')

    def json(self):
        return json.loads(self.content)


class FakeBambuddy:
    def __init__(self):
        self.uploads = {}
        self.queue = {}
        self.started = []
        self.fail = {}
        self.printers = {}          # id -> {"serial_number", "connected", ...}
        self.deleted = []
        self.connect_new_printers = True
        self.status_checks = 0

    def _maybe_fail(self, what):
        if what in self.fail:
            raise BambuddyError(self.fail[what])

    def upload_file(self, fileobj, filename):
        self._maybe_fail('upload')
        file_id = str(100 + len(self.uploads))
        self.uploads[file_id] = (filename, fileobj.read())
        return file_id

    def enqueue(self, file_id, printer_id, manual_start):
        self._maybe_fail('enqueue')
        queue_id = str(500 + len(self.queue))
        self.queue[queue_id] = {'file': file_id, 'printer': printer_id,
                                'manual_start': manual_start, 'status': 'pending'}
        return queue_id

    def start_item(self, queue_id):
        self._maybe_fail('start')
        self.started.append(queue_id)

    def queue_item_status(self, queue_id):
        item = self.queue.get(queue_id)
        return item['status'] if item else johncad_agent.MISSING

    def add_printer(self, name, ip_address, access_code, serial_number):
        self._maybe_fail('add_printer')
        self.printers[30 + len(self.printers)] = {
            'name': name, 'ip_address': ip_address, 'access_code': access_code,
            'serial_number': serial_number, 'connected': self.connect_new_printers,
        }

    def find_printer_id(self, serial_number):
        self._maybe_fail('list_printers')
        return next((pid for pid, printer in self.printers.items()
                     if printer['serial_number'] == serial_number), None)

    def printer_connected(self, printer_id):
        self.status_checks += 1
        return self.printers[printer_id]['connected']

    def delete_printer(self, printer_id):
        self._maybe_fail('delete_printer')
        del self.printers[printer_id]
        self.deleted.append(printer_id)


class AgentProgramTests(AgentTestBase):
    def setUp(self):
        super().setUp()
        self.state_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state_dir, ignore_errors=True)
        self.config = johncad_agent.Config({
            'JOHNCAD_URL': 'https://print.test', 'JOHNCAD_AGENT_KEY': self.key,
            'BAMBUDDY_API_KEY': 'k', 'BAMBUDDY_PRINTER_ID': '1',
            'AGENT_STATE_DIR': str(self.state_dir),
        })
        self.config.chunk_size = 4096   # PAYLOAD takes 3 chunks
        self.session = TestClientSession(self.client)
        self.bambuddy = FakeBambuddy()
        self.prog = self.make_agent()
        sleep = mock.patch('johncad_agent.time.sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def make_agent(self):
        return johncad_agent.Agent(
            self.config,
            home=johncad_agent.Home(self.config, session=self.session),
            bambuddy=self.bambuddy,
            state=johncad_agent.State(self.state_dir),
        )

    def test_full_cycle(self):
        job = self.sliced()
        self.assertEqual(self.prog.run_once(), 15)
        filename, data = self.bambuddy.uploads['100']
        self.assertEqual((filename, data), (f'johncad-job-{job.id}.gcode.3mf', PAYLOAD))
        self.assertEqual(self.bambuddy.queue['500'],
                         {'file': '100', 'printer': 4, 'manual_start': True, 'status': 'pending'})
        ranges = [r for m, p, r in self.session.calls if p.endswith('/file/')]
        self.assertEqual(ranges, ['bytes=0-4095', 'bytes=4096-8191', 'bytes=8192-10499'])
        job.refresh_from_db()
        self.assertEqual((job.bambuddy_queue_id, job.dispatch_state), ('500', 'staged'))
        self.assertEqual(list((self.state_dir / 'files').iterdir()), [])

        # Staff release, the agent starts it and tracks the print.
        self.client.force_login(self.teacher)
        self.client.post(reverse('queue-release-next'), {'printer': self.x1c.pk})
        self.client.logout()
        self.prog.run_once()
        self.assertEqual(self.bambuddy.started, ['500'])
        job.refresh_from_db()
        self.assertEqual(job.dispatch_state, 'released')

        self.bambuddy.queue['500']['status'] = 'printing'
        self.prog.run_once()
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.WORKING)
        self.bambuddy.queue['500']['status'] = 'completed'
        self.prog.run_once()
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.FINISHED)
        self.prog.run_once()          # nothing left to do
        self.assertEqual(len(self.bambuddy.queue), 1)

    def test_unassigned_job_uses_agent_default_printer(self):
        self.sliced(printer=None)
        self.prog.run_once()
        self.assertEqual(self.bambuddy.queue['500']['printer'], '1')

    def test_dropped_connection_resumes_download(self):
        job = self.sliced()

        real_chunk = self.prog.home.chunk
        calls = {'n': 0}

        def flaky_chunk(job_id, start, end):
            calls['n'] += 1
            if calls['n'] >= 2:     # first chunk ok, then the network dies
                raise johncad_agent.HomeError('connection reset by peer')
            return real_chunk(job_id, start, end)

        with mock.patch.object(self.prog.home, 'chunk', side_effect=flaky_chunk):
            self.prog.run_once()
        self.assertEqual(self.bambuddy.uploads, {})
        self.assertEqual((self.state_dir / 'files' / f'job-{job.id}.part').stat().st_size, 4096)
        job.refresh_from_db()
        self.assertEqual(job.dispatch_state, 'dispatching')   # still our claim

        # Next round, with a fresh agent (as after a restart): resumes at 4096.
        self.session.calls.clear()
        self.make_agent().run_once()
        ranges = [r for m, p, r in self.session.calls if p.endswith('/file/')]
        self.assertEqual(ranges, ['bytes=4096-8191', 'bytes=8192-10499'])
        self.assertEqual(self.bambuddy.uploads['100'][1], PAYLOAD)

    def test_transient_chunk_errors_are_retried(self):
        self.sliced()
        real_request = self.session.request
        state = {'failed': False}

        def request(method, url, **kwargs):
            if url.endswith('/file/') and not state['failed']:
                state['failed'] = True
                raise johncad_agent.requests.ConnectionError('reset')
            return real_request(method, url, **kwargs)

        with mock.patch.object(self.session, 'request', side_effect=request):
            self.prog.run_once()
        self.assertEqual(self.bambuddy.uploads['100'][1], PAYLOAD)

    def test_changed_file_restarts_download(self):
        job = self.sliced()
        part = self.state_dir / 'files' / f'job-{job.id}.part'
        meta = self.state_dir / 'files' / f'job-{job.id}.json'
        part.write_bytes(b'x' * 4096)
        meta.write_text(json.dumps({'sha256': 'old', 'size': 10500}))
        self.prog.run_once()
        self.assertEqual(self.bambuddy.uploads['100'][1], PAYLOAD)

    def test_unreachable_site_keeps_report_until_it_is_back(self):
        job = self.sliced()
        real_post = self.prog.home.post

        def post(path, body=None):
            if path.endswith('/dispatched/'):
                raise johncad_agent.HomeError('site down')
            return real_post(path, body)

        with mock.patch.object(self.prog.home, 'post', side_effect=post):
            with self.assertRaises(johncad_agent.HomeError):
                self.prog.run_once()
        self.assertEqual(len(self.bambuddy.queue), 1)
        job.refresh_from_db()
        self.assertEqual(job.bambuddy_queue_id, '')

        # After a restart the saved report goes first; the job isn't re-sent
        # even though the site still lists it as waiting.
        self.make_agent().run_once()
        job.refresh_from_db()
        self.assertEqual(job.bambuddy_queue_id, '500')
        self.assertEqual(len(self.bambuddy.queue), 1)
        self.assertEqual(len(self.bambuddy.uploads), 1)

    def test_enqueue_failure_reported_and_upload_kept_for_retry(self):
        job = self.sliced()
        self.bambuddy.fail['enqueue'] = 'printer 4 not found'
        self.prog.run_once()
        job.refresh_from_db()
        self.assertEqual(job.dispatch_state, 'failed')
        self.assertIn('printer 4 not found', job.dispatch_error)
        self.assertEqual(job.bambuddy_file_id, '100')
        # Teacher retries: the agent reuses the uploaded file.
        del self.bambuddy.fail['enqueue']
        self.client.force_login(self.teacher)
        self.client.post(reverse('job-review-retry-dispatch', args=[job.pk]))
        self.client.logout()
        self.prog.run_once()
        job.refresh_from_db()
        self.assertEqual(job.bambuddy_queue_id, '500')
        self.assertEqual(len(self.bambuddy.uploads), 1)

    def test_failed_enqueue_of_reused_file_forgets_it(self):
        job = self.sliced(bambuddy_file_id='42')
        self.bambuddy.fail['enqueue'] = 'library file 42 not found'
        self.prog.run_once()
        job.refresh_from_db()
        self.assertEqual(job.bambuddy_file_id, '')

    def test_release_failure_is_reported(self):
        job = self.dispatched(bambuddy_queue_id='500', release_requested_at=timezone.now())
        self.bambuddy.queue['500'] = {'status': 'pending'}
        self.bambuddy.fail['start'] = 'Bambuddy POST /queue/500/start returned 409: spool short'
        self.prog.run_once()
        job.refresh_from_db()
        self.assertEqual(job.dispatch_state, 'staged')
        self.assertIn('spool short', job.release_error)

    def test_failed_print_reported(self):
        job = self.dispatched(bambuddy_queue_id='500', status=Job.Status.WORKING)
        self.bambuddy.queue['500'] = {'status': 'cancelled'}
        self.prog.run_once()
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertEqual(job.dispatch_error, 'Bambuddy queue item is "cancelled".')

    def test_missing_printer_id_is_reported(self):
        self.config.default_printer_id = None
        job = self.sliced(printer=None)
        self.prog.run_once()
        job.refresh_from_db()
        self.assertIn('BAMBUDDY_PRINTER_ID', job.dispatch_error)

    def test_only_one_agent_per_state_dir(self):
        lock = johncad_agent.acquire_lock(self.state_dir)
        self.assertIsNotNone(lock)
        self.addCleanup(lock.close)
        self.assertIsNone(johncad_agent.acquire_lock(self.state_dir))

    def test_config_problems(self):
        self.config.chunk_size = 4 * 1024 * 1024
        self.assertEqual(self.config.problems(), [])
        bad = johncad_agent.Config({'JOHNCAD_URL': 'http://print.test'})
        problems = ' '.join(bad.problems())
        self.assertIn('JOHNCAD_AGENT_KEY is not set', problems)
        self.assertIn('BAMBUDDY_API_KEY is not set', problems)
        self.assertIn('https://', problems)

    # -- adding printers ---------------------------------------------------

    def printer_ticket(self, **kwargs):
        model = PrinterModel.objects.get_or_create(
            name='X1 Carbon 0.4', defaults={'settings_path': 'm.json;p.json'},
        )[0]
        fields = {
            'name': 'Lab X1C', 'ip_address': '192.168.1.50', 'access_code': 'CODE1234',
            'serial_number': '01P00A000000002', 'printer_model': model,
            'requested_by': self.teacher, **kwargs,
        }
        return AddPrinterTicket.objects.create(**fields)

    def test_add_printer_ticket_full_cycle(self):
        ticket = self.printer_ticket()
        self.prog.run_once()
        self.assertEqual(self.bambuddy.printers[30], {
            'name': 'Lab X1C', 'ip_address': '192.168.1.50', 'access_code': 'CODE1234',
            'serial_number': '01P00A000000002', 'connected': True,
        })
        ticket.refresh_from_db()
        self.assertEqual(
            (ticket.status, ticket.bambuddy_printer_id, ticket.access_code, ticket.agent),
            (AddPrinterTicket.Status.SUCCEEDED, 30, '', self.agent),
        )
        printer = Printer.objects.get(name='Lab X1C')
        self.assertEqual(
            (printer.bambuddy_printer_id, printer.serial_number, ticket.printer),
            (30, '01P00A000000002', printer),
        )
        self.assertEqual(self.prog.state.outbox, [])
        # Nothing more to do next round, and no second printer in Bambuddy.
        self.prog.run_once()
        self.assertEqual(len(self.bambuddy.printers), 1)

    def test_new_printer_then_prints_a_job(self):
        self.printer_ticket()
        self.prog.run_once()
        printer = Printer.objects.get(name='Lab X1C')
        self.sliced(printer=printer)
        self.prog.run_once()
        self.assertEqual(self.bambuddy.queue['500']['printer'], 30)

    def test_printer_that_never_connects_fails_and_is_removed(self):
        self.bambuddy.connect_new_printers = False
        ticket = self.printer_ticket()
        self.prog.run_once()
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, AddPrinterTicket.Status.FAILED)
        self.assertIn('could not connect to the printer within 60 seconds', ticket.error)
        self.assertEqual((self.bambuddy.printers, self.bambuddy.deleted), ({}, [30]))
        self.assertEqual(self.bambuddy.status_checks, 20)      # 60 s / every 3 s
        self.assertFalse(Printer.objects.filter(name='Lab X1C').exists())

    def test_printer_that_connects_late_succeeds(self):
        ticket = self.printer_ticket()
        self.bambuddy.connect_new_printers = False
        original = self.bambuddy.printer_connected

        def connected(printer_id):
            if self.bambuddy.status_checks == 3:
                self.bambuddy.printers[printer_id]['connected'] = True
            return original(printer_id)

        self.bambuddy.printer_connected = connected
        self.prog.run_once()
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, AddPrinterTicket.Status.SUCCEEDED)
        self.assertEqual(self.bambuddy.status_checks, 4)

    def test_bambuddy_refusing_the_printer_is_reported_without_the_access_code(self):
        self.bambuddy.fail['add_printer'] = 'Bambuddy POST /printers/ returned 400: bad code CODE1234'
        ticket = self.printer_ticket()
        self.prog.run_once()
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, AddPrinterTicket.Status.FAILED)
        self.assertIn('returned 400', ticket.error)
        self.assertNotIn('CODE1234', ticket.error)
        self.assertEqual(self.bambuddy.printers, {})

    def test_printer_already_in_bambuddy_is_adopted_not_added_twice(self):
        # e.g. an earlier attempt added it and was cut off before reporting.
        self.bambuddy.printers[41] = {'serial_number': '01P00A000000002', 'connected': True}
        ticket = self.printer_ticket()
        self.prog.run_once()
        ticket.refresh_from_db()
        self.assertEqual(
            (ticket.status, ticket.bambuddy_printer_id), (AddPrinterTicket.Status.SUCCEEDED, 41))
        self.assertEqual(list(self.bambuddy.printers), [41])

    def test_existing_bambuddy_printer_is_not_deleted_when_it_is_offline(self):
        self.bambuddy.printers[41] = {'serial_number': '01P00A000000002', 'connected': False}
        ticket = self.printer_ticket()
        self.prog.run_once()
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, AddPrinterTicket.Status.FAILED)
        self.assertEqual((list(self.bambuddy.printers), self.bambuddy.deleted), ([41], []))

    def test_printer_report_survives_site_outage_and_restart(self):
        ticket = self.printer_ticket()
        original = self.session.request

        def flaky(method, url, **kwargs):
            if url.endswith('/done/'):
                raise johncad_agent.requests.ConnectionError('site down')
            return original(method, url, **kwargs)

        self.session.request = flaky
        with self.assertRaises(johncad_agent.HomeError):
            self.prog.run_once()
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, AddPrinterTicket.Status.ADDING)

        self.session.request = original
        self.prog = self.make_agent()        # restart: the report is on disk
        self.prog.run_once()
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, AddPrinterTicket.Status.SUCCEEDED)
        self.assertEqual(len(self.bambuddy.printers), 1)

    def test_cancelled_ticket_is_skipped(self):
        ticket = self.printer_ticket()
        work = {'printer_tickets': [{
            'id': ticket.id, 'name': ticket.name, 'ip_address': ticket.ip_address,
            'access_code': ticket.access_code, 'serial_number': ticket.serial_number,
        }]}
        from . import printer_tickets
        printer_tickets.cancel(ticket)
        self.prog.add_printer(work['printer_tickets'][0])
        self.assertEqual(self.bambuddy.printers, {})
