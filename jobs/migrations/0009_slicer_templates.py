"""Slice from a template .3mf instead of loading preset .json files.

Adds PrinterModel.template_path and Printer.template_path, and stops using
PrinterModel.settings_path and Filament.preset_path.

The two old columns are removed from Django's view of the models but are NOT
dropped from the database yet, so a site rolled back to the previous release
(a code checkout, never a schema rollback: see CONTRIBUTING.md) still finds
them with the values they had. They get a database default of '' so rows
created from now on are valid. A later release drops the columns.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0008_convert_printer_presets'),
    ]

    operations = [
        migrations.AddField(
            model_name='printermodel',
            name='template_path',
            field=models.CharField(blank=True, max_length=1000, help_text=(
                'Path on the site Pi of the OrcaSlicer .3mf project used as the '
                'template for this kind of printer (it holds the machine, process '
                'and filament settings). Blank = ORCA_SLICER_TEMPLATE from .env. '
                'A single printer can use its own template instead.'
            )),
        ),
        migrations.AddField(
            model_name='printer',
            name='template_path',
            field=models.CharField(blank=True, max_length=1000, help_text=(
                'Path on the site Pi of an OrcaSlicer .3mf template for this '
                "printer only. Blank = its printer model's template, or "
                'ORCA_SLICER_TEMPLATE from .env.'
            )),
        ),
        migrations.AlterField(
            model_name='printer',
            name='printer_model',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=models.deletion.PROTECT,
                related_name='printers', to='jobs.printermodel',
                help_text='Decides the slicer template, unless this printer has its own.',
            ),
        ),
        migrations.AlterField(
            model_name='printermodel',
            name='settings_path',
            field=models.CharField(blank=True, db_default='', max_length=1000),
        ),
        migrations.AlterField(
            model_name='filament',
            name='preset_path',
            field=models.CharField(blank=True, db_default='', max_length=500),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name='printermodel', name='settings_path'),
                migrations.RemoveField(model_name='filament', name='preset_path'),
            ],
        ),
    ]
