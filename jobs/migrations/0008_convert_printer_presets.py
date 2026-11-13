"""Move each printer's preset paths into PrinterModel / Filament rows, then
stop using the old Printer.slicer_settings and Printer.slicer_filaments.

The two old columns are removed from Django's view of the model but are NOT
dropped from the database yet, so a site rolled back to the previous release
(a code checkout, never a schema rollback: see CONTRIBUTING.md) still finds
them. 0007 gave them a database default of '' so printers created from now on
get a valid value. A later release drops the columns.
"""
from pathlib import PurePosixPath

from django.db import migrations

MAX_SLOTS = 4


def _unique(base, taken, limit):
    """`base` cut to `limit` characters, with " (2)", " (3)"... if taken."""
    name, n = base[:limit], 1
    while name in taken:
        n += 1
        suffix = f' ({n})'
        name = base[:limit - len(suffix)] + suffix
    taken.add(name)
    return name


def presets_to_rows(apps, schema_editor):
    Printer = apps.get_model('jobs', 'Printer')
    PrinterModel = apps.get_model('jobs', 'PrinterModel')
    Filament = apps.get_model('jobs', 'Filament')
    PrinterFilament = apps.get_model('jobs', 'PrinterFilament')

    model_names = set(PrinterModel.objects.values_list('name', flat=True))
    filament_colors = set(
        Filament.objects.filter(filament_type='Unknown').values_list('color', flat=True),
    )
    for printer in Printer.objects.exclude(slicer_settings=''):
        printer_model = PrinterModel.objects.filter(settings_path=printer.slicer_settings).first()
        if printer_model is None:
            # The old data has no model name; an admin can rename it.
            printer_model = PrinterModel.objects.create(
                name=_unique(f'{printer.name} presets', model_names, 100),
                settings_path=printer.slicer_settings,
            )
        printer.printer_model = printer_model
        printer.save(update_fields=['printer_model'])

        paths = [p.strip() for p in printer.slicer_filaments.split(';') if p.strip()]
        for slot, path in enumerate(paths[:MAX_SLOTS], start=1):
            filament = Filament.objects.filter(preset_path=path).first()
            if filament is None:
                # Type and color aren't known from a path; an admin fills them in.
                stem = PurePosixPath(path.replace('\\', '/')).stem or 'filament'
                filament = Filament.objects.create(
                    filament_type='Unknown',
                    color=_unique(stem, filament_colors, 50),
                    preset_path=path,
                )
            # get_or_create: safe to run again after this migration is reversed.
            PrinterFilament.objects.get_or_create(
                printer=printer, slot=slot, defaults={'filament': filament},
            )


def rows_to_presets(apps, schema_editor):
    Printer = apps.get_model('jobs', 'Printer')
    PrinterFilament = apps.get_model('jobs', 'PrinterFilament')
    for printer in Printer.objects.exclude(printer_model=None).select_related('printer_model'):
        printer.slicer_settings = printer.printer_model.settings_path
        printer.slicer_filaments = ';'.join(
            PrinterFilament.objects.filter(printer=printer)
            .order_by('slot').values_list('filament__preset_path', flat=True)
        )
        printer.save(update_fields=['slicer_settings', 'slicer_filaments'])


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0007_printer_models_and_tickets'),
    ]

    operations = [
        migrations.RunPython(presets_to_rows, rows_to_presets),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name='printer', name='slicer_filaments'),
                migrations.RemoveField(model_name='printer', name='slicer_settings'),
            ],
        ),
    ]
