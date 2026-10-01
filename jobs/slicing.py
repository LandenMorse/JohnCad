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


def slice_to_gcode(job):
    """
    Convert job.attachment's .stl into G-code via the OrcaSlicer CLI and
    attach the result to job.gcode.

    On success, advances job.status to Job.Status.QUEUED and saves -- callers
    (jobs/admin.py) rely on that contract.
    """
    if not settings.ORCA_SLICER_SETTINGS:
        raise SlicerNotConfigured('ORCA_SLICER_SETTINGS is not set (machine/process presets).')
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
            '--load-settings', settings.ORCA_SLICER_SETTINGS,
        ]
        if settings.ORCA_SLICER_FILAMENTS:
            command += ['--load-filaments', settings.ORCA_SLICER_FILAMENTS]
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

        # Orca names its output itself (e.g. plate_1.gcode).
        gcode_files = sorted(Path(tmp_dir).glob('*.gcode'))
        if not gcode_files:
            raise SlicingFailed('OrcaSlicer exited successfully but produced no G-code file.')

        with gcode_files[0].open('rb') as f:
            job.gcode.save(f'{job.id}.gcode', File(f), save=False)

    job.status = Job.Status.QUEUED
    job.save(update_fields=['gcode', 'status'])
