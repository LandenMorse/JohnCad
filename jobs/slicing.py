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
    Convert job.attachment's .stl into G-code via the PrusaSlicer CLI and
    attach the result to job.gcode.

    On success, advances job.status to Job.Status.QUEUED and saves -- callers
    (jobs/admin.py) rely on that contract.
    """
    stl_path = job.attachment.file.path

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / f'{job.id}.gcode'

        command = [settings.PRUSA_SLICER_BIN, '--export-gcode', '--output', str(out_path)]
        if settings.PRUSA_SLICER_PROFILE:
            command += ['--load', settings.PRUSA_SLICER_PROFILE]
        command.append(stl_path)

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=settings.PRUSA_SLICER_TIMEOUT,
            )
        except FileNotFoundError as exc:
            raise SlicerNotConfigured(
                f'PrusaSlicer binary "{settings.PRUSA_SLICER_BIN}" not found on PATH.'
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SlicingFailed(
                f'Slicing timed out after {settings.PRUSA_SLICER_TIMEOUT}s.'
            ) from exc

        if result.returncode != 0:
            raise SlicingFailed(
                f'PrusaSlicer exited {result.returncode}: {result.stderr.strip() or result.stdout.strip()}'
            )

        if not out_path.exists():
            raise SlicingFailed('PrusaSlicer exited successfully but produced no G-code file.')

        with out_path.open('rb') as f:
            job.gcode.save(f'{job.id}.gcode', File(f), save=False)

    job.status = Job.Status.QUEUED
    job.save(update_fields=['gcode', 'status'])
