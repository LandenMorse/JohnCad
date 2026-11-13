"""Printer models, filaments, the teacher /printers/ page and the
"add printer" tickets the print agent carries out (jobs/printer_tickets.py)."""
import json
from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import printer_tickets
from .forms import AddPrinterTicketForm, PrinterFilamentsForm
from .models import (
    AddPrinterTicket,
    Filament,
    Job,
    Printer,
    PrinterAgent,
    PrinterFilament,
    PrinterModel,
)
from .slicing import SlicerNotConfigured, slice_to_3mf
from .tests import ReviewTestBase, fake_orca

Status = AddPrinterTicket.Status
API = '/agent-api/v1/'
VALID = {
    'name': 'Lab X1C',
    'ip_address': '192.168.1.50',
    'access_code': '12345678',
    'serial_number': '01P00A000000002',
}


class PrinterTestBase(ReviewTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.x1c_model = PrinterModel.objects.create(
            name='X1 Carbon 0.4', settings_path='x1c-machine.json;x1c-process.json',
        )
        cls.pla_black = Filament.objects.create(
            filament_type='PLA', color='Black', preset_path='pla-black.json',
        )
        cls.petg_white = Filament.objects.create(
            filament_type='PETG', color='White', preset_path='petg-white.json',
        )

    def ticket(self, **kwargs):
        fields = {**VALID, 'printer_model': self.x1c_model, 'requested_by': self.teacher, **kwargs}
        return AddPrinterTicket.objects.create(**fields)

    def form_data(self, **kwargs):
        return {**VALID, 'printer_model': self.x1c_model.pk, **kwargs}


@override_settings(ORCA_SLICER_CMD='orca')
class PrinterModelSlicingTests(PrinterTestBase):
    def slice(self, job):
        commands = []
        with mock.patch('jobs.slicing.subprocess.run', side_effect=fake_orca(commands)):
            slice_to_3mf(job)
        return commands[0]

    def printer(self, **kwargs):
        kwargs.setdefault('printer_model', self.x1c_model)
        return Printer.objects.create(name='X1C', bambuddy_printer_id=1, **kwargs)

    @override_settings(ORCA_SLICER_SETTINGS='env-m.json;env-p.json', ORCA_SLICER_FILAMENTS='env-f.json')
    def test_model_presets_and_slot_filaments_replace_env(self):
        printer = self.printer()
        # Created out of order: slot order, not creation order, is what counts.
        PrinterFilament.objects.create(printer=printer, filament=self.petg_white, slot=3)
        PrinterFilament.objects.create(printer=printer, filament=self.pla_black, slot=1)
        command = self.slice(self.make_job(status=Job.Status.APPROVED, printer=printer))
        self.assertEqual(
            command[command.index('--load-settings') + 1], 'x1c-machine.json;x1c-process.json',
        )
        self.assertEqual(
            command[command.index('--load-filaments') + 1], 'pla-black.json;petg-white.json',
        )

    @override_settings(ORCA_SLICER_SETTINGS='env-m.json;env-p.json', ORCA_SLICER_FILAMENTS='env-f.json')
    def test_model_presets_never_mix_with_env_filaments(self):
        command = self.slice(self.make_job(status=Job.Status.APPROVED, printer=self.printer()))
        self.assertNotIn('--load-filaments', command)

    @override_settings(ORCA_SLICER_SETTINGS='env-m.json;env-p.json', ORCA_SLICER_FILAMENTS='env-f.json')
    def test_printer_without_model_falls_back_to_env_pair(self):
        printer = self.printer(printer_model=None)
        command = self.slice(self.make_job(status=Job.Status.APPROVED, printer=printer))
        self.assertEqual(command[command.index('--load-settings') + 1], 'env-m.json;env-p.json')
        self.assertEqual(command[command.index('--load-filaments') + 1], 'env-f.json')

    @override_settings(ORCA_SLICER_SETTINGS='')
    def test_printer_without_model_or_env_is_not_configured(self):
        job = self.make_job(status=Job.Status.APPROVED, printer=self.printer(printer_model=None))
        with self.assertRaisesMessage(SlicerNotConfigured, 'printer model'):
            slice_to_3mf(job)


class PrinterFilamentModelTests(PrinterTestBase):
    def setUp(self):
        self.printer = Printer.objects.create(
            name='X1C', bambuddy_printer_id=1, printer_model=self.x1c_model,
        )

    def test_one_filament_per_slot(self):
        PrinterFilament.objects.create(printer=self.printer, filament=self.pla_black, slot=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PrinterFilament.objects.create(printer=self.printer, filament=self.petg_white, slot=1)

    def test_slot_must_be_1_to_4(self):
        with self.assertRaises(ValidationError):
            PrinterFilament(printer=self.printer, filament=self.pla_black, slot=5).full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            PrinterFilament.objects.create(printer=self.printer, filament=self.pla_black, slot=5)

    def test_filament_type_and_color_are_unique_together(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Filament.objects.create(filament_type='PLA', color='Black', preset_path='other.json')

    def test_filaments_form_replaces_slots(self):
        PrinterFilament.objects.create(printer=self.printer, filament=self.pla_black, slot=1)
        form = PrinterFilamentsForm(
            {'slot_2': self.petg_white.pk, 'slot_4': self.pla_black.pk}, printer=self.printer,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(
            list(self.printer.filament_slots.values_list('slot', 'filament__color')),
            [(2, 'White'), (4, 'Black')],
        )
        self.assertEqual(self.printer.filament_preset_paths(), 'petg-white.json;pla-black.json')


class AddPrinterTicketFormTests(PrinterTestBase):
    def test_valid(self):
        form = AddPrinterTicketForm(self.form_data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_serial_number_is_normalised(self):
        form = AddPrinterTicketForm(self.form_data(serial_number=' 01p00a 000000002 '))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['serial_number'], '01P00A000000002')

    def test_bad_values_are_rejected(self):
        for field, value in (
            ('ip_address', 'printer.local'),
            ('ip_address', '999.1.1.1'),
            ('access_code', '1234'),
            ('access_code', ''),
            ('serial_number', '01P00A-0000'),
            ('printer_model', ''),
            ('name', ''),
        ):
            with self.subTest(field=field, value=value):
                form = AddPrinterTicketForm(self.form_data(**{field: value}))
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)

    def test_name_and_serial_must_be_new(self):
        Printer.objects.create(
            name='lab x1c', bambuddy_printer_id=1, serial_number='01P00A000000002',
        )
        form = AddPrinterTicketForm(self.form_data())
        self.assertFalse(form.is_valid())
        self.assertEqual(set(form.errors), {'name', 'serial_number'})

    def test_open_ticket_blocks_a_duplicate_but_a_failed_one_does_not(self):
        ticket = self.ticket()
        self.assertFalse(AddPrinterTicketForm(self.form_data()).is_valid())
        printer_tickets.fail(ticket, self.make_agent()[0], 'wrong code')
        self.assertTrue(AddPrinterTicketForm(self.form_data()).is_valid())

    def make_agent(self):
        agent = PrinterAgent(name='school-pi')
        key = agent.set_new_key()
        agent.save()
        return agent, key


@override_settings(BAMBUDDY_VIA_AGENT=True)
class PrintersPageTests(PrinterTestBase):
    def test_teachers_only(self):
        response = self.client.get(reverse('printers'))
        self.assertRedirects(response, '/login/?next=/printers/')
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse('printers')).status_code, 403)
        self.assertEqual(self.client.post(reverse('printers'), self.form_data()).status_code, 403)
        self.assertFalse(AddPrinterTicket.objects.exists())
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get(reverse('printers')).status_code, 200)

    def test_nav_link_is_for_staff(self):
        self.client.force_login(self.student)
        self.assertNotContains(self.client.get(reverse('queue')), reverse('printers'))
        self.client.force_login(self.teacher)
        self.assertContains(self.client.get(reverse('queue')), reverse('printers'))

    def test_submitting_creates_a_pending_ticket(self):
        self.client.force_login(self.teacher)
        response = self.client.post(reverse('printers'), self.form_data())
        self.assertRedirects(response, reverse('printers'))
        ticket = AddPrinterTicket.objects.get()
        self.assertEqual(
            (ticket.name, ticket.status, ticket.requested_by, ticket.printer_model),
            ('Lab X1C', Status.PENDING, self.teacher, self.x1c_model),
        )
        self.assertFalse(Printer.objects.exists())

    def test_invalid_form_is_shown_again_with_errors(self):
        self.client.force_login(self.teacher)
        response = self.client.post(reverse('printers'), self.form_data(access_code='123'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '8 letters and digits')
        self.assertFalse(AddPrinterTicket.objects.exists())
        # No reload under someone correcting the form.
        self.assertNotContains(response, 'http-equiv="refresh"')

    def test_page_shows_tickets_but_never_the_access_code(self):
        self.ticket(access_code='SECRET99')
        failed = self.ticket(name='Broken', serial_number='01P00A000000009')
        agent = PrinterAgent(name='school-pi')
        agent.set_new_key()
        agent.save()
        printer_tickets.fail(failed, agent, 'Bambuddy could not connect to the printer.')
        self.client.force_login(self.teacher)
        response = self.client.get(reverse('printers'))
        self.assertContains(response, 'Lab X1C')
        self.assertContains(response, 'Waiting for the print agent')
        self.assertContains(response, 'Bambuddy could not connect to the printer.')
        self.assertNotContains(response, 'SECRET99')
        self.assertContains(response, 'http-equiv="refresh"')

    def test_page_stops_refreshing_when_nothing_is_open(self):
        self.client.force_login(self.teacher)
        self.assertNotContains(self.client.get(reverse('printers')), 'http-equiv="refresh"')

    def test_warns_when_no_agent_exists(self):
        self.client.force_login(self.teacher)
        self.assertContains(self.client.get(reverse('printers')), 'No print agent is set up yet')

    def test_cancel_pending_ticket(self):
        ticket = self.ticket()
        self.client.force_login(self.teacher)
        self.client.post(reverse('printer-ticket-cancel', args=[ticket.pk]))
        ticket.refresh_from_db()
        self.assertEqual((ticket.status, ticket.access_code), (Status.CANCELLED, ''))

    def test_cannot_cancel_once_picked_up(self):
        ticket = self.ticket(status=Status.ADDING)
        self.client.force_login(self.teacher)
        response = self.client.post(reverse('printer-ticket-cancel', args=[ticket.pk]), follow=True)
        self.assertContains(response, 'already been picked up')
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Status.ADDING)

    def test_teacher_sets_filaments(self):
        printer = Printer.objects.create(
            name='X1C', bambuddy_printer_id=1, printer_model=self.x1c_model,
        )
        self.client.force_login(self.student)
        url = reverse('printer-filaments', args=[printer.pk])
        data = {f'printer-{printer.pk}-slot_1': self.pla_black.pk}
        self.assertEqual(self.client.post(url, data).status_code, 403)
        self.client.force_login(self.teacher)
        self.assertRedirects(self.client.post(url, data), reverse('printers'))
        self.assertEqual(printer.filament_preset_paths(), 'pla-black.json')
        response = self.client.get(reverse('printers'))
        self.assertContains(response, f'value="{self.pla_black.pk}" selected')


@override_settings(BAMBUDDY_VIA_AGENT=True, AGENT_CLAIM_TIMEOUT=900)
class AgentPrinterTicketApiTests(PrinterTestBase):
    def setUp(self):
        self.agent = PrinterAgent(name='school-pi')
        self.key = self.agent.set_new_key()
        self.agent.save()

    def api(self, method, path, body=None, key=None):
        headers = {'HTTP_AUTHORIZATION': f'Bearer {self.key if key is None else key}'}
        if method == 'get':
            return self.client.get(API + path, **headers)
        return self.client.post(
            API + path, data=json.dumps(body or {}), content_type='application/json', **headers,
        )

    def test_poll_lists_open_tickets_with_what_the_agent_needs(self):
        ticket = self.ticket()
        self.ticket(name='Done', serial_number='01P00A000000003', status=Status.SUCCEEDED)
        self.assertEqual(self.api('get', 'poll/').json()['printer_tickets'], [{
            'id': ticket.id,
            'name': 'Lab X1C',
            'ip_address': '192.168.1.50',
            'access_code': '12345678',
            'serial_number': '01P00A000000002',
        }])

    def test_needs_a_valid_key(self):
        ticket = self.ticket()
        for path in ('claim/', 'done/', 'failed/'):
            response = self.api('post', f'printer-tickets/{ticket.id}/{path}', key='jca_wrong')
            self.assertEqual(response.status_code, 401)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Status.PENDING)

    def test_claim_is_repeatable_for_the_same_agent_only(self):
        ticket = self.ticket()
        self.assertEqual(self.api('post', f'printer-tickets/{ticket.id}/claim/').status_code, 200)
        self.assertEqual(self.api('post', f'printer-tickets/{ticket.id}/claim/').status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual((ticket.status, ticket.agent), (Status.ADDING, self.agent))

        other = PrinterAgent(name='other-pi')
        other_key = other.set_new_key()
        other.save()
        response = self.api('post', f'printer-tickets/{ticket.id}/claim/', key=other_key)
        self.assertEqual(response.status_code, 409)
        # ...and the other agent isn't offered it either.
        self.assertEqual(self.api('get', 'poll/', key=other_key).json()['printer_tickets'], [])
        self.assertEqual(len(self.api('get', 'poll/').json()['printer_tickets']), 1)

    def test_done_creates_the_printer_and_blanks_the_access_code(self):
        ticket = self.ticket(status=Status.ADDING, agent=self.agent)
        response = self.api('post', f'printer-tickets/{ticket.id}/done/', {'bambuddy_printer_id': 7})
        self.assertEqual(response.json(), {'status': 'ok'})
        ticket.refresh_from_db()
        printer = Printer.objects.get()
        self.assertEqual(
            (printer.name, printer.bambuddy_printer_id, printer.serial_number,
             printer.printer_model, printer.is_active),
            ('Lab X1C', 7, '01P00A000000002', self.x1c_model, True),
        )
        self.assertEqual(
            (ticket.status, ticket.printer, ticket.bambuddy_printer_id, ticket.access_code),
            (Status.SUCCEEDED, printer, 7, ''),
        )
        self.assertIsNotNone(ticket.completed_at)

        # A repeated report changes nothing.
        response = self.api('post', f'printer-tickets/{ticket.id}/done/', {'bambuddy_printer_id': 7})
        self.assertEqual(response.json(), {'status': 'unchanged'})
        self.assertEqual(Printer.objects.count(), 1)

    def test_done_needs_a_positive_printer_id(self):
        ticket = self.ticket(status=Status.ADDING, agent=self.agent)
        for body in ({}, {'bambuddy_printer_id': 'abc'}, {'bambuddy_printer_id': 0}):
            response = self.api('post', f'printer-tickets/{ticket.id}/done/', body)
            self.assertEqual(response.status_code, 400)
        self.assertFalse(Printer.objects.exists())

    def test_new_printer_is_usable_on_review_and_by_a_scoped_agent(self):
        old = Printer.objects.create(name='Old', bambuddy_printer_id=1)
        self.agent.printers.add(old)
        ticket = self.ticket(status=Status.ADDING, agent=self.agent)
        self.api('post', f'printer-tickets/{ticket.id}/done/', {'bambuddy_printer_id': 7})
        printer = Printer.objects.get(name='Lab X1C')
        self.assertIn(printer, self.agent.printers.all())
        self.client.force_login(self.teacher)
        self.make_job(status=Job.Status.PENDING)
        self.assertContains(self.client.get(reverse('job-review')), 'Lab X1C')

    def test_done_fails_the_ticket_if_the_printer_is_already_here(self):
        Printer.objects.create(name='Existing', bambuddy_printer_id=7)
        ticket = self.ticket(status=Status.ADDING, agent=self.agent)
        self.api('post', f'printer-tickets/{ticket.id}/done/', {'bambuddy_printer_id': 7})
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Status.FAILED)
        self.assertIn('"Existing"', ticket.error)
        self.assertEqual(Printer.objects.count(), 1)

    def test_done_fails_the_ticket_if_the_name_was_taken_meanwhile(self):
        ticket = self.ticket(status=Status.ADDING, agent=self.agent)
        Printer.objects.create(name='Lab X1C', bambuddy_printer_id=1)
        self.api('post', f'printer-tickets/{ticket.id}/done/', {'bambuddy_printer_id': 7})
        ticket.refresh_from_db()
        self.assertEqual((ticket.status, ticket.access_code), (Status.FAILED, ''))
        self.assertIn('already exists', ticket.error)

    def test_failed_records_the_reason(self):
        ticket = self.ticket(status=Status.ADDING, agent=self.agent)
        response = self.api(
            'post', f'printer-tickets/{ticket.id}/failed/', {'error': 'Wrong access code.'},
        )
        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(
            (ticket.status, ticket.error, ticket.access_code),
            (Status.FAILED, 'Wrong access code.', ''),
        )
        self.assertFalse(Printer.objects.exists())
        # Closed: neither report applies any more.
        self.assertEqual(
            self.api('post', f'printer-tickets/{ticket.id}/failed/', {'error': 'x'}).status_code, 409)
        self.assertEqual(
            self.api('post', f'printer-tickets/{ticket.id}/done/',
                     {'bambuddy_printer_id': 7}).status_code, 409)

    def test_cancelled_ticket_is_not_offered_or_completed(self):
        ticket = self.ticket()
        printer_tickets.cancel(ticket)
        self.assertEqual(self.api('get', 'poll/').json()['printer_tickets'], [])
        self.assertEqual(self.api('post', f'printer-tickets/{ticket.id}/claim/').status_code, 409)

    def test_stale_claim_is_offered_again(self):
        other = PrinterAgent(name='gone-pi')
        other.set_new_key()
        other.save()
        ticket = self.ticket(
            status=Status.ADDING, agent=other, claimed_at=timezone.now() - timedelta(seconds=901),
        )
        fresh = self.ticket(
            name='Fresh', serial_number='01P00A000000004', status=Status.ADDING, agent=other,
            claimed_at=timezone.now(),
        )
        offered = [t['id'] for t in self.api('get', 'poll/').json()['printer_tickets']]
        self.assertEqual(offered, [ticket.id])
        fresh.refresh_from_db()
        self.assertEqual(fresh.status, Status.ADDING)

    @override_settings(BAMBUDDY_VIA_AGENT=False)
    def test_api_is_off_without_agent_mode(self):
        ticket = self.ticket()
        self.assertEqual(self.api('post', f'printer-tickets/{ticket.id}/claim/').status_code, 404)


class PresetMigrationTests(TransactionTestCase):
    """0008 turns each printer's old preset path columns into PrinterModel and
    Filament rows, and can be reversed."""
    BEFORE = [('jobs', '0007_printer_models_and_tickets')]
    AFTER = [('jobs', '0008_convert_printer_presets')]

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        return MigrationExecutor(connection).loader.project_state(target).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_presets_become_rows_and_back(self):
        apps = self.migrate(self.BEFORE)
        OldPrinter = apps.get_model('jobs', 'Printer')
        OldPrinter.objects.create(
            name='X1C-1', bambuddy_printer_id=1,
            slicer_settings='/orca/x1c-m.json;/orca/x1c-p.json',
            slicer_filaments='/orca/pla.json;/orca/petg.json',
        )
        OldPrinter.objects.create(
            name='X1C-2', bambuddy_printer_id=2,
            slicer_settings='/orca/x1c-m.json;/orca/x1c-p.json',
            slicer_filaments='/orca/pla.json',
        )
        OldPrinter.objects.create(name='Env', bambuddy_printer_id=3)

        apps = self.migrate(self.AFTER)
        NewPrinter = apps.get_model('jobs', 'Printer')
        NewModel = apps.get_model('jobs', 'PrinterModel')
        NewSlot = apps.get_model('jobs', 'PrinterFilament')
        # Two printers with the same presets share one model.
        model = NewModel.objects.get()
        self.assertEqual(
            (model.name, model.settings_path),
            ('X1C-1 presets', '/orca/x1c-m.json;/orca/x1c-p.json'),
        )
        self.assertEqual(NewPrinter.objects.get(name='X1C-2').printer_model_id, model.pk)
        self.assertIsNone(NewPrinter.objects.get(name='Env').printer_model_id)
        self.assertEqual(
            list(NewSlot.objects.filter(printer__name='X1C-1')
                 .order_by('slot').values_list('slot', 'filament__preset_path', 'filament__color')),
            [(1, '/orca/pla.json', 'pla'), (2, '/orca/petg.json', 'petg')],
        )
        self.assertEqual(apps.get_model('jobs', 'Filament').objects.count(), 2)
        # The current code slices with exactly what the old columns said.
        printer = Printer.objects.get(name='X1C-1')
        self.assertEqual(printer.printer_model.settings_path, '/orca/x1c-m.json;/orca/x1c-p.json')
        self.assertEqual(printer.filament_preset_paths(), '/orca/pla.json;/orca/petg.json')
        # The old columns are still in the database for a rolled-back release,
        # and new printers get a valid value in them.
        Printer.objects.create(name='New', bambuddy_printer_id=9)
        with connection.cursor() as cursor:
            cursor.execute('SELECT slicer_settings FROM jobs_printer ORDER BY name')
            self.assertEqual(
                [row[0] for row in cursor.fetchall()],
                ['', '', '/orca/x1c-m.json;/orca/x1c-p.json', '/orca/x1c-m.json;/orca/x1c-p.json'],
            )

        apps = self.migrate(self.BEFORE)
        back = apps.get_model('jobs', 'Printer').objects.get(name='X1C-1')
        self.assertEqual(back.slicer_filaments, '/orca/pla.json;/orca/petg.json')
