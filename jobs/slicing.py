import os
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


def template_for(job):
    """Path of the template .3mf to slice `job` into.

    The job's printer's own template wins, then its printer model's, then
    ORCA_SLICER_TEMPLATE. `~` and `$HOME` are expanded.
    """
    printer = job.printer
    candidates = []
    if printer is not None:
        candidates.append(printer.template_path)
        if printer.printer_model_id:
            candidates.append(printer.printer_model.template_path)
    candidates.append(settings.ORCA_SLICER_TEMPLATE)
    for candidate in candidates:
        if candidate and candidate.strip():
            return os.path.expanduser(os.path.expandvars(candidate.strip()))
    raise SlicerNotConfigured(
        'No slicer template: set ORCA_SLICER_TEMPLATE in .env, or a template on '
        'the printer or its printer model in /admin/.'
    )


def slice_to_3mf(job):
    """
    Slice job.attachment's .stl via the OrcaSlicer CLI into a .gcode.3mf and
    attach it to job.sliced_file.

    The command is
    `<ORCA_SLICER_CMD> --arrange 1 --slice 0 --export-3mf <output> <template.3mf> <stl>`:
    the template .3mf supplies the machine, process and filament settings (see
    `template_for`), the .stl is added to it and arranged on the plate.

    On success, advances job.status to Job.Status.QUEUED and saves -- the
    slice and dispatch workers rely on that contract.

    The .gcode.3mf is the file that is sent to Bambuddy (docs/bambuddy.md).
    """
    template = template_for(job)
    base_command = shlex.split(settings.ORCA_SLICER_CMD)
    if not base_command:
        raise SlicerNotConfigured('ORCA_SLICER_CMD is empty.')
    if not Path(template).is_file():
        printer = job.printer
        where = f' for printer "{printer.name}"' if printer is not None else ''
        raise SlicerNotConfigured(
            f'Slicer template{where} not found: {template}. Fix the path in /admin/ '
            '(printer or printer model) or ORCA_SLICER_TEMPLATE in .env.'
        )

    stl_path = job.attachment.file.path

    # The scratch dir lives under MEDIA_ROOT, not the system temp dir: the
    # Flatpak sandbox has a private /tmp but can be granted MEDIA_ROOT.
    scratch_root = Path(settings.MEDIA_ROOT) / 'slicing_tmp'
    scratch_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=scratch_root) as tmp_dir:
        command = base_command + [
            '--arrange', '1',
            '--slice', '0',
            '--export-3mf', str(Path(tmp_dir) / OUTPUT_NAME),
        ]
        if settings.ORCA_SLICER_DATADIR:
            command += ['--datadir', settings.ORCA_SLICER_DATADIR]
        # The template goes before the .stl; Orca loads both into one plate.
        command += [template, stl_path]

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=settings.ORCA_SLICER_TIMEOUT,
                # Anything else Orca writes lands in the scratch dir and is
                # removed with it.
                cwd=tmp_dir,
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
