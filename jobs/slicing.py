import shlex
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.files import File

from .models import Job


class SlicerNotConfigured(NotImplementedError):
    pass


class SlicingFailed(Exception):
    pass


# Name Orca is told to write; Bambu printers (via Bambuddy) take a sliced
# .gcode.3mf, not bare G-code.
OUTPUT_NAME = 'output.gcode.3mf'


def _require_readable(path, what):
    if not Path(path).is_file():
        raise SlicerNotConfigured(
            f'{what} is missing on the server ({path}). Upload it again in /admin/.'
        )


def _presets_for(job):
    """(machine+process presets, filament presets) to slice `job` with.

    A printer with a PrinterModel uses that model's uploaded machine and
    process .json files (or, for models set up before uploads existed, its
    legacy settings path) and the filaments loaded in the printer's slots, in
    slot order, each an uploaded .json (or legacy path). One without a model
    (or a job with no printer) falls back to the ORCA_SLICER_* settings as a
    pair, so a printer's machine preset is never mixed with another printer's
    filaments.
    """
    printer = job.printer
    if printer is not None and printer.printer_model_id:
        model = printer.printer_model
        presets = model.settings_arg()
        if not presets:
            raise SlicerNotConfigured(
                f'Printer model "{model.name}" has no machine and process presets: '
                'upload them in /admin/.'
            )
        if model.uses_uploaded_files:
            _require_readable(model.machine_file.path, f'The machine preset of "{model.name}"')
            _require_readable(model.process_file.path, f'The process preset of "{model.name}"')
        filaments = []
        for slot in printer.filament_slots.select_related('filament'):
            filament = slot.filament
            location = filament.preset_location()
            if not location:
                raise SlicerNotConfigured(
                    f'Filament "{filament}" (slot {slot.slot} of "{printer.name}") has no '
                    'preset: upload one in /admin/.'
                )
            if filament.preset_file:
                _require_readable(location, f'The preset of filament "{filament}"')
            filaments.append(location)
        return presets, ';'.join(filaments)
    if not settings.ORCA_SLICER_SETTINGS:
        if printer is not None:
            raise SlicerNotConfigured(
                f'No slicer presets for printer "{printer.name}": give it a printer '
                'model in /admin/ (or set ORCA_SLICER_SETTINGS).'
            )
        raise SlicerNotConfigured('ORCA_SLICER_SETTINGS is not set (machine/process presets).')
    return settings.ORCA_SLICER_SETTINGS, settings.ORCA_SLICER_FILAMENTS


def slice_to_3mf(job):
    """
    Slice job.attachment's .stl via the OrcaSlicer CLI into a .gcode.3mf and
    attach it to job.sliced_file.

    On success, advances job.status to Job.Status.QUEUED and saves -- the
    slice and dispatch workers rely on that contract.

    `--export-3mf` alongside `--slice 0` produces the file that is sent to
    Bambuddy (docs/bambuddy.md); this is the path used in production.
    """
    presets, filaments = _presets_for(job)
    base_command = shlex.split(settings.ORCA_SLICER_CMD)
    if not base_command:
        raise SlicerNotConfigured('ORCA_SLICER_CMD is empty.')

    stl_path = job.attachment.file.path

    # The scratch dir lives under MEDIA_ROOT, not the system temp dir: the
    # Flatpak sandbox has a private /tmp but can be granted MEDIA_ROOT.
    scratch_root = Path(settings.MEDIA_ROOT) / 'slicing_tmp'
    scratch_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=scratch_root) as tmp_dir:
        command = base_command + [
            '--slice', '0',
            '--outputdir', tmp_dir,
            '--export-3mf', OUTPUT_NAME,
            '--load-settings', presets,
        ]
        if filaments:
            command += ['--load-filaments', filaments]
        if settings.ORCA_SLICER_DATADIR:
            command += ['--datadir', settings.ORCA_SLICER_DATADIR]
        command.append(stl_path)

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=settings.ORCA_SLICER_TIMEOUT,
            )
        except FileNotFoundError as exc:
            raise SlicerNotConfigured(
                f'OrcaSlicer command "{base_command[0]}" not found on PATH.'
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SlicingFailed(
                f'Slicing timed out after {settings.ORCA_SLICER_TIMEOUT}s.'
            ) from exc

        if result.returncode != 0:
            raise SlicingFailed(
                f'OrcaSlicer exited {result.returncode}: {result.stderr.strip() or result.stdout.strip()}'
            )

        # Prefer the requested name, but accept whatever .3mf Orca wrote.
        output = Path(tmp_dir) / OUTPUT_NAME
        if not output.exists():
            found = sorted(Path(tmp_dir).glob('*.3mf'))
            if not found:
                raise SlicingFailed('OrcaSlicer exited successfully but produced no .3mf file.')
            output = found[0]

        with output.open('rb') as f:
            job.sliced_file.save(f'{job.id}.gcode.3mf', File(f), save=False)

    job.status = Job.Status.QUEUED
    job.save(update_fields=['sliced_file', 'status'])
