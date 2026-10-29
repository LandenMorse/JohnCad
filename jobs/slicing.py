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


def _presets_for(job):
    """(machine+process presets, filament presets) to slice `job` with.

    A printer with its own presets uses only those. One without (or a job with
    no printer) falls back to the ORCA_SLICER_* settings as a pair, so a
    printer's machine preset is never mixed with another printer's filaments.
    """
    printer = job.printer
    if printer is not None and printer.slicer_settings:
        return printer.slicer_settings, printer.slicer_filaments
    if not settings.ORCA_SLICER_SETTINGS:
        if printer is not None:
            raise SlicerNotConfigured(
                f'No slicer presets for printer "{printer.name}": set them on the '
                'printer in /admin/ (or set ORCA_SLICER_SETTINGS).'
            )
        raise SlicerNotConfigured('ORCA_SLICER_SETTINGS is not set (machine/process presets).')
    return settings.ORCA_SLICER_SETTINGS, settings.ORCA_SLICER_FILAMENTS


def slice_to_3mf(job):
    """
    Slice job.attachment's .stl via the OrcaSlicer CLI into a .gcode.3mf and
    attach it to job.sliced_file.

    On success, advances job.status to Job.Status.QUEUED and saves -- the
    slice and dispatch workers rely on that contract.

    UNVERIFIED: `--export-3mf` alongside `--slice 0` is taken from the CLI's
    --help; confirm on the Pi that the result prints when hand-uploaded to
    Bambuddy (docs/BAMBUDDY_PLAN.md, build order step 1).
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
