"""Replace printer models and filaments with a library of slicer templates.

Adds SlicerTemplate (an uploaded OrcaSlicer project .3mf), the templates each
Printer offers with its default, and the template chosen for each Job.
PrinterModel, Filament and PrinterFilament are deleted, with the template
paths added in 0009.

Unlike 0008 and 0009 this DROPS the old tables and columns (including the
columns those two left behind), so the previous release does not run against
a database migrated past here: restore the backup update.sh takes to go back.
Nothing is converted: preset paths can't become templates, so every printer
has no template until a teacher uploads one on /printers/.
"""
import django.core.validators
import django.db.models.deletion
import jobs.threemf
from django.conf import settings
from django.db import migrations, models


LEFTOVER_PRINTER_COLUMNS = ('slicer_settings', 'slicer_filaments')


def _printer_columns(apps, schema_editor):
    connection = schema_editor.connection
    table = apps.get_model('jobs', 'Printer')._meta.db_table
    with connection.cursor() as cursor:
        description = connection.introspection.get_table_description(cursor, table)
    return schema_editor.quote_name(table), {column.name for column in description}


def drop_leftover_printer_columns(apps, schema_editor):
    """0008 stopped using these two columns but left them in the database.
    SQLite has already lost them (it rebuilds the table above), hence the
    check."""
    table, existing = _printer_columns(apps, schema_editor)
    for column in LEFTOVER_PRINTER_COLUMNS:
        if column in existing:
            schema_editor.execute(
                f'ALTER TABLE {table} DROP COLUMN {schema_editor.quote_name(column)}'
            )


def restore_leftover_printer_columns(apps, schema_editor):
    """Put the columns back (empty), as reversing 0008 writes to them."""
    table, existing = _printer_columns(apps, schema_editor)
    for column in LEFTOVER_PRINTER_COLUMNS:
        if column not in existing:
            schema_editor.execute(
                f'ALTER TABLE {table} ADD COLUMN {schema_editor.quote_name(column)} '
                "varchar(1000) DEFAULT '' NOT NULL"
            )


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0009_slicer_templates'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SlicerTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(help_text='What teachers see when choosing, e.g. "X1C PLA 0.2mm".', max_length=100, unique=True)),
                ('file', models.FileField(help_text='An OrcaSlicer project saved with File > Save Project As. Anything left on its plate is printed with every job, so save it with an empty plate.', upload_to='slicer_templates/', validators=[django.core.validators.FileExtensionValidator(allowed_extensions=['3mf']), jobs.threemf.validate_slicer_template], verbose_name='project file')),
                ('machine_preset', models.CharField(blank=True, editable=False, max_length=255)),
                ('process_preset', models.CharField(blank=True, editable=False, max_length=255)),
                ('filament_presets', models.JSONField(blank=True, default=list, editable=False)),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['name'],
            },
        ),
        migrations.RemoveConstraint(
            model_name='filament',
            name='unique_filament_type_color',
        ),
        migrations.RemoveField(
            model_name='printer',
            name='filaments',
        ),
        migrations.RemoveConstraint(
            model_name='printerfilament',
            name='unique_printer_filament_slot',
        ),
        migrations.RemoveConstraint(
            model_name='printerfilament',
            name='printer_filament_slot_1_to_4',
        ),
        migrations.RemoveField(
            model_name='addprinterticket',
            name='printer_model',
        ),
        migrations.RemoveField(
            model_name='printer',
            name='printer_model',
        ),
        migrations.RemoveField(
            model_name='printer',
            name='template_path',
        ),
        migrations.AlterField(
            model_name='printer',
            name='is_active',
            field=models.BooleanField(default=True, help_text='Untick to stop offering this printer to teachers (keeps job history).', verbose_name='in use'),
        ),
        migrations.AddField(
            model_name='slicertemplate',
            name='uploaded_by',
            field=models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='slicer_templates', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='job',
            name='template',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='jobs', to='jobs.slicertemplate'),
        ),
        migrations.AddField(
            model_name='printer',
            name='default_template',
            field=models.ForeignKey(blank=True, help_text='The template offered first when approving a job for this printer.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='default_for', to='jobs.slicertemplate'),
        ),
        migrations.AddField(
            model_name='printer',
            name='templates',
            field=models.ManyToManyField(blank=True, help_text='The slicer templates teachers can choose for this printer.', related_name='printers', to='jobs.slicertemplate'),
        ),
        migrations.RemoveField(
            model_name='printerfilament',
            name='filament',
        ),
        migrations.RemoveField(
            model_name='printerfilament',
            name='printer',
        ),
        migrations.DeleteModel(
            name='PrinterModel',
        ),
        migrations.DeleteModel(
            name='Filament',
        ),
        migrations.DeleteModel(
            name='PrinterFilament',
        ),
        migrations.RunPython(drop_leftover_printer_columns, restore_leftover_printer_columns),
    ]
