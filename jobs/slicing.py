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


# What OrcaSlicer's CLI exit codes mean (CLI_* in OrcaSlicer's
# src/libslic3r/Utils.hpp), for the ones a template or a model can cause.
EXIT_REASONS = {
    -3: 'a file it was given could not be found',
    -15: 'the template asks to change the printer, which the command line cannot do',
    -16: "the template's printer is not supported by this OrcaSlicer",
    -17: "the template's process settings do not suit its printer",
    -18: 'the template has invalid values in its settings',
    -21: 'the model could not be arranged on the plate (it may be too big)',
    -24: (
        'the template was saved by a newer OrcaSlicer or Bambu Studio than the one on '
        'the server. Update OrcaSlicer on the site Pi (flatpak update '
        'com.orcaslicer.OrcaSlicer), or save the template again with the same version'
    ),
    -50: 'there was nothing on the plate it could print',
    -51: "the model failed OrcaSlicer's checks for this printer",
    -52: 'the model sticks out past the edge of the plate',
    -61: "a filament in the template can't be used on the template's bed type",
    -62: 'the filaments in the template need temperatures too far apart',
    -100: 'slicing failed',
    -101: 'the print head would hit something on the plate',
    -102: 'part of the print is in an area the printer cannot reach',
}
# The end of OrcaSlicer's output usually says what went wrong; the rest is
# progress noise.
OUTPUT_TAIL_LINES = 15


def _signed_exit_code(returncode):
    """OrcaSlicer returns negative codes; Linux reports -24 as 232, Windows
    as 4294967272."""
    if returncode > 2 ** 31:
        return returncode - 2 ** 32
    if returncode > 127:
        return returncode - 256
    return returncode


def describe_failure(result, template):
    """The slicing error for an OrcaSlicer run that exited non-zero."""
    code = _signed_exit_code(result.returncode)
    message = f'OrcaSlicer exited {result.returncode}'
    if code != result.returncode:
        message += f' ({code})'
    reason = EXIT_REASONS.get(code)
    if reason:
        message += f': {reason}.'
    if code == -24 and template.saved_with:
        message += f' The template was saved with {template.saved_with}.'
    output = (result.stderr or result.stdout or '').strip().splitlines()
    if output:
        message += '\n' + '\n'.join(output[-OUTPUT_TAIL_LINES:])
    return message


# Name Orca is told to write; Bambu printers (via Bambuddy) take a sliced
# .gcode.3mf, not bare G-code.
OUTPUT_NAME = 'output.gcode.3mf'


def template_path_for(job):
    """Path of the template .3mf to slice `job` into: the SlicerTemplate the
    teacher chose when approving it."""
    template = job.template
    if template is None:
        raise SlicerNotConfigured(
            'No slicer template was chosen for this job (or it has since been '
            'deleted). Retry it and choose a template.'
        )
    path = Path(template.file.path)
    if not path.is_file():
        raise SlicerNotConfigured(
            f'The file of slicer template "{template.name}" is missing on the server '
            f'({path}). Upload the template again on the Printers page.'
        )
    return str(path)


def slice_to_3mf(job):
    """
    Slice job.attachment's .stl via the OrcaSlicer CLI into a .gcode.3mf and
    attach it to job.sliced_file.

    The command is
    `<ORCA_SLICER_CMD> --arrange 1 --slice 0 --export-3mf <output> <template.3mf> <stl>`:
    the template .3mf (job.template, see `template_path_for`) supplies the
    machine, process and filament settings, the .stl is added to it and
    arranged on the plate.

    On success, advances job.status to Job.Status.QUEUED and saves -- the
    slice and dispatch workers rely on that contract.

    The .gcode.3mf is the file that is sent to Bambuddy (docs/bambuddy.md).
    """
    template = template_path_for(job)
    base_command = shlex.split(settings.ORCA_SLICER_CMD)
    if not base_command:
        raise SlicerNotConfigured('ORCA_SLICER_CMD is empty.')

    stl_path = job.attachment.file.path

    # The scratch dir lives under MEDIA_ROOT, not the system temp dir: the
    # Flatpak sandbox has a private /tmp but can be granted MEDIA_ROOT (which
    # is also where the templates and the .stl files are).
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
            raise SlicingFailed(describe_failure(result, job.template))

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
