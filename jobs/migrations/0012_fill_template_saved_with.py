"""Fill SlicerTemplate.saved_with for templates uploaded before 0011.

A file that is missing or can't be read is left blank; it only affects what
the Printers page shows.
"""
from django.core.exceptions import ValidationError
from django.db import migrations

from jobs.threemf import read_presets


def fill_saved_with(apps, schema_editor):
    SlicerTemplate = apps.get_model('jobs', 'SlicerTemplate')
    for template in SlicerTemplate.objects.filter(saved_with=''):
        try:
            with template.file.open('rb') as file:
                saved_with = read_presets(file).saved_with
        except (OSError, ValidationError):
            continue
        if saved_with:
            template.saved_with = saved_with
            template.save(update_fields=['saved_with'])


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0011_slicertemplate_saved_with'),
    ]

    operations = [
        migrations.RunPython(fill_saved_with, migrations.RunPython.noop),
    ]
