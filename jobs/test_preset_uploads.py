"""Machine, process and filament presets uploaded as .json files in /admin/,
and slicing with them."""
import json
import os
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from .models import Filament, Job, Printer, PrinterFilament, PrinterModel
from .slicing import SlicerNotConfigured, slice_to_3mf
from .tests import ReviewTestBase, fake_orca


def preset(name, kind='machine', **extra):
    body = {'name': name, 'type': kind, **extra}
    return SimpleUploadedFile(f'{name}.json', json.dumps(body).encode(), 'application/json')


def raw(name, data):
    return SimpleUploadedFile(name, data, 'application/json')


class PresetValidationTests(ReviewTestBase):
    def model(self, **kwargs):
        return PrinterModel(name='X1C', **kwargs)

    def test_both_files_make_a_valid_model(self):
        self.model(machine_file=preset('m'), process_file=preset('p', 'process')).full_clean()

    def test_legacy_path_alone_is_still_valid(self):
        self.model(settings_path='m.json;p.json').full_clean()

    def test_one_file_without_the_other_is_rejected(self):
        with self.assertRaisesMessage(ValidationError, 'both the machine and the process'):
            self.model(machine_file=preset('m')).full_clean()
        with self.assertRaisesMessage(ValidationError, 'both the machine and the process'):
            self.model(process_file=preset('p', 'process')).full_clean()

    def test_nothing_at_all_is_rejected(self):
        with self.assertRaisesMessage(ValidationError, 'Upload a machine preset'):
            self.model().full_clean()

    def test_invalid_json_is_rejected(self):
        model = self.model(machine_file=raw('m.json', b'{not json'), process_file=preset('p', 'process'))
        with self.assertRaisesMessage(ValidationError, 'not a valid JSON file'):
            model.full_clean()

    def test_json_that_is_not_an_object_is_rejected(self):
        model = self.model(machine_file=raw('m.json', b'[1, 2]'), process_file=preset('p', 'process'))
        with self.assertRaisesMessage(ValidationError, 'expected a JSON object'):
            model.full_clean()

    def test_a_process_preset_cannot_be_uploaded_as_the_machine(self):
        model = self.model(machine_file=preset('p', 'process'), process_file=preset('p2', 'process'))
        with self.assertRaisesMessage(ValidationError, 'is a "process" preset'):
            model.full_clean()

    def test_a_preset_that_does_not_declare_its_type_is_accepted(self):
        model = self.model(
            machine_file=raw('m.json', b'{"name": "x"}'), process_file=raw('p.json', b'{}'),
        )
        model.full_clean()

    def test_only_json_files(self):
        model = self.model(machine_file=raw('m.txt', b'{}'), process_file=preset('p', 'process'))
        with self.assertRaises(ValidationError) as caught:
            model.full_clean()
        self.assertIn('machine_file', caught.exception.message_dict)

    def test_filament_needs_a_file_or_a_legacy_path(self):
        with self.assertRaisesMessage(ValidationError, 'Upload a filament preset'):
            Filament(filament_type='PLA', color='Black').full_clean()
        Filament(filament_type='PLA', color='Black', preset_file=preset('f', 'filament')).full_clean()
        Filament(filament_type='PLA', color='Black', preset_path='f.json').full_clean()

    def test_a_machine_preset_cannot_be_uploaded_as_a_filament(self):
        with self.assertRaisesMessage(ValidationError, 'is a "machine" preset'):
            Filament(filament_type='PLA', color='Black', preset_file=preset('f')).full_clean()


class AdminUploadTests(ReviewTestBase):
    def test_superuser_uploads_the_files_for_a_printer_model(self):
        self.client.force_login(self.root)
        response = self.client.post('/admin/jobs/printermodel/add/', {
            'name': 'X1 Carbon 0.4',
            'machine_file': preset('x1c-machine'),
            'process_file': preset('x1c-process', 'process'),
        })
        self.assertEqual(response.status_code, 302, getattr(response, 'context', None) and
                         response.context['adminform'].form.errors)
        model = PrinterModel.objects.get(name='X1 Carbon 0.4')
        self.assertTrue(model.uses_uploaded_files)
        with open(model.machine_file.path, encoding='utf-8') as f:
            self.assertEqual(json.load(f)['name'], 'x1c-machine')
        self.assertEqual(self.client.get(f'/admin/jobs/printermodel/{model.pk}/change/').status_code, 200)

    def test_a_bad_upload_shows_an_error_and_saves_nothing(self):
        self.client.force_login(self.root)
        response = self.client.post('/admin/jobs/printermodel/add/', {
            'name': 'X1 Carbon 0.4',
            'machine_file': raw('m.json', b'{not json'),
            'process_file': preset('p', 'process'),
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'not a valid JSON file')
        self.assertFalse(PrinterModel.objects.exists())

    def test_superuser_uploads_a_filament_preset(self):
        self.client.force_login(self.root)
        response = self.client.post('/admin/jobs/filament/add/', {
            'filament_type': 'PLA', 'color': 'Black', 'preset_file': preset('pla', 'filament'),
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Filament.objects.get(color='Black').preset_file)

    def test_teachers_cannot_upload(self):
        self.client.force_login(self.teacher)
        response = self.client.get('/admin/jobs/printermodel/add/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response.url)


@override_settings(ORCA_SLICER_CMD='orca')
class SlicingWithUploadedPresetsTests(ReviewTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.model = PrinterModel.objects.create(
            name='X1C', machine_file=preset('x1c-machine'),
            process_file=preset('x1c-process', 'process'),
        )
        cls.pla = Filament.objects.create(
            filament_type='PLA', color='Black', preset_file=preset('pla-black', 'filament'),
        )
        cls.petg = Filament.objects.create(
            filament_type='PETG', color='White', preset_file=preset('petg-white', 'filament'),
        )

    def slice(self, job):
        commands = []
        with mock.patch('jobs.slicing.subprocess.run', side_effect=fake_orca(commands)):
            slice_to_3mf(job)
        return commands[0]

    def printer(self, **kwargs):
        kwargs.setdefault('printer_model', self.model)
        return Printer.objects.create(name='X1C', bambuddy_printer_id=1, **kwargs)

    def job(self, printer):
        return self.make_job(status=Job.Status.APPROVED, printer=printer)

    def test_the_uploaded_machine_and_process_files_are_loaded(self):
        command = self.slice(self.job(self.printer()))
        machine, process = command[command.index('--load-settings') + 1].split(';')
        self.assertEqual(machine, self.model.machine_file.path)
        self.assertEqual(process, self.model.process_file.path)
        # The files the slicer is given really are the uploaded ones.
        with open(machine, encoding='utf-8') as f:
            self.assertEqual(json.load(f)['name'], 'x1c-machine')
        with open(process, encoding='utf-8') as f:
            self.assertEqual(json.load(f)['type'], 'process')

    def test_uploaded_filaments_are_loaded_in_slot_order(self):
        printer = self.printer()
        # Created out of order: slot order, not creation order, is what counts.
        PrinterFilament.objects.create(printer=printer, filament=self.petg, slot=3)
        PrinterFilament.objects.create(printer=printer, filament=self.pla, slot=1)
        command = self.slice(self.job(printer))
        self.assertEqual(
            command[command.index('--load-filaments') + 1],
            f'{self.pla.preset_file.path};{self.petg.preset_file.path}',
        )

    def test_no_filaments_loaded_means_no_load_filaments(self):
        self.assertNotIn('--load-filaments', self.slice(self.job(self.printer())))

    def test_uploaded_files_beat_the_legacy_path(self):
        model = PrinterModel.objects.create(
            name='Both', settings_path='old-m.json;old-p.json',
            machine_file=preset('new-m'), process_file=preset('new-p', 'process'),
        )
        command = self.slice(self.job(self.printer(printer_model=model)))
        self.assertNotIn('old-m.json', command[command.index('--load-settings') + 1])
        self.assertEqual(
            command[command.index('--load-settings') + 1],
            f'{model.machine_file.path};{model.process_file.path}',
        )

    def test_a_legacy_model_and_filament_still_slice_by_path(self):
        legacy = PrinterModel.objects.create(name='Legacy', settings_path='old-m.json;old-p.json')
        old_filament = Filament.objects.create(
            filament_type='ABS', color='Grey', preset_path='old-abs.json',
        )
        printer = self.printer(printer_model=legacy)
        PrinterFilament.objects.create(printer=printer, filament=old_filament, slot=1)
        PrinterFilament.objects.create(printer=printer, filament=self.pla, slot=2)
        command = self.slice(self.job(printer))
        self.assertEqual(command[command.index('--load-settings') + 1], 'old-m.json;old-p.json')
        self.assertEqual(
            command[command.index('--load-filaments') + 1],
            f'old-abs.json;{self.pla.preset_file.path}',
        )

    def test_the_selected_printers_model_decides_which_files(self):
        other = PrinterModel.objects.create(
            name='P1S', machine_file=preset('p1s-m'), process_file=preset('p1s-p', 'process'),
        )
        first = self.printer()
        second = Printer.objects.create(name='P1S', bambuddy_printer_id=2, printer_model=other)
        first_command = self.slice(self.job(first))
        second_command = self.slice(self.job(second))
        self.assertIn('x1c-machine', first_command[first_command.index('--load-settings') + 1])
        self.assertIn('p1s-m', second_command[second_command.index('--load-settings') + 1])

    def test_a_model_with_no_presets_is_not_configured(self):
        empty = PrinterModel.objects.create(name='Empty')
        with self.assertRaisesMessage(SlicerNotConfigured, 'Printer model "Empty"'):
            slice_to_3mf(self.job(self.printer(printer_model=empty)))

    def test_a_filament_with_no_preset_names_the_filament(self):
        bare = Filament.objects.create(filament_type='TPU', color='Red')
        printer = self.printer()
        PrinterFilament.objects.create(printer=printer, filament=bare, slot=2)
        with self.assertRaisesMessage(SlicerNotConfigured, 'Filament "TPU Red" (slot 2'):
            slice_to_3mf(self.job(printer))

    def test_an_uploaded_file_missing_from_disk_asks_for_a_new_upload(self):
        # Its own model: the shared one's files must stay on disk for other tests.
        model = PrinterModel.objects.create(
            name='Lost', machine_file=preset('lost-m'), process_file=preset('lost-p', 'process'),
        )
        os.remove(model.machine_file.path)
        with mock.patch('jobs.slicing.subprocess.run') as run:
            with self.assertRaisesMessage(SlicerNotConfigured, 'machine preset of "Lost" is missing'):
                slice_to_3mf(self.job(self.printer(printer_model=model)))
        run.assert_not_called()
