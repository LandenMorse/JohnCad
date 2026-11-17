"""Reading an uploaded OrcaSlicer project (.3mf) used as a slicer template.

A .3mf is a zip archive. One saved as a project by OrcaSlicer (or Bambu Studio)
holds its machine, process and filament settings in
`Metadata/project_settings.config`, a JSON object; that file is what makes a
.3mf usable as a template. No Django models are imported here, so
`jobs/models.py` can use this module.
"""
import json
import re
import zipfile
from typing import NamedTuple

from django.core.exceptions import ValidationError
from django.template.defaultfilters import filesizeformat

SETTINGS_MEMBER = 'Metadata/project_settings.config'
MODEL_MEMBER = '3D/3dmodel.model'
# The <metadata> elements come before the mesh, so the start of the model
# file is enough to find which program saved the project.
MODEL_HEAD_BYTES = 64 * 1024
APPLICATION_RE = re.compile(rb'<metadata\s+name="Application"\s*>\s*([^<]{1,100}?)\s*</metadata>')
MAX_TEMPLATE_BYTES = 50 * 1024 * 1024
# The settings file is a few hundred kB; anything far larger is not one.
MAX_SETTINGS_BYTES = 5 * 1024 * 1024


class Presets(NamedTuple):
    """What a template was saved with, for showing to teachers: the preset
    names, and the program that saved it (e.g. "OrcaSlicer-2.5.1"). OrcaSlicer
    refuses to slice a project saved by a newer version than itself."""
    machine: str = ''
    process: str = ''
    filaments: tuple = ()
    saved_with: str = ''


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _saved_with(archive):
    try:
        with archive.open(MODEL_MEMBER) as model:
            head = model.read(MODEL_HEAD_BYTES)
    except KeyError:
        return ''
    match = APPLICATION_RE.search(head)
    return match.group(1).decode('utf-8', 'replace') if match else ''


def read_presets(file):
    """The Presets named in an OrcaSlicer project .3mf.

    `file` is any seekable binary file (an upload or a stored FieldFile); it is
    left at its start. Raises ValidationError if it is not a .3mf project with
    slicer settings.
    """
    if file.size > MAX_TEMPLATE_BYTES:
        raise ValidationError(
            'This file is %(size)s; a template can be at most %(limit)s.',
            code='too_large',
            params={
                'size': filesizeformat(file.size),
                'limit': filesizeformat(MAX_TEMPLATE_BYTES),
            },
        )
    try:
        file.seek(0)
        with zipfile.ZipFile(file) as archive:
            try:
                info = archive.getinfo(SETTINGS_MEMBER)
            except KeyError:
                raise ValidationError(
                    'This .3mf has no slicer settings in it. In OrcaSlicer, set up the '
                    'printer, filament and process, then use File > Save Project As.',
                    code='no_settings',
                ) from None
            if info.file_size > MAX_SETTINGS_BYTES:
                raise ValidationError(
                    'The slicer settings in this .3mf are too large to read.',
                    code='settings_too_large',
                )
            settings = json.loads(archive.read(info))
            saved_with = _saved_with(archive)
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise ValidationError(
            'This is not a valid .3mf project file.', code='not_a_project',
        ) from exc
    finally:
        file.seek(0)

    if not isinstance(settings, dict):
        raise ValidationError('This is not a valid .3mf project file.', code='not_a_project')
    filaments = settings.get('filament_settings_id')
    if isinstance(filaments, str):
        filaments = [filaments]
    elif not isinstance(filaments, list):
        filaments = []
    return Presets(
        machine=_text(settings.get('printer_settings_id')),
        process=_text(settings.get('print_settings_id')),
        filaments=tuple(name for name in map(_text, filaments) if name),
        saved_with=saved_with,
    )


def validate_slicer_template(file):
    """Model field validator: `file` must be an OrcaSlicer project .3mf."""
    read_presets(file)
