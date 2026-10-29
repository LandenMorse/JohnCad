from django.db import migrations, models

import jobs.models


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0003_job_classroom'),
    ]

    operations = [
        migrations.RenameField(
            model_name='job',
            old_name='gcode',
            new_name='sliced_file',
        ),
        migrations.AlterField(
            model_name='job',
            name='sliced_file',
            field=models.FileField(blank=True, upload_to=jobs.models.job_sliced_file_upload_path),
        ),
        migrations.AlterField(
            model_name='job',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('approved', 'Approved'),
                    ('queued', 'Queued'),
                    ('working', 'Working'),
                    ('finished', 'Finished'),
                    ('rejected', 'Rejected'),
                    ('failed', 'Failed'),
                ],
                default='pending',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='job',
            name='bambuddy_file_id',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='job',
            name='bambuddy_queue_id',
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        migrations.AddField(
            model_name='job',
            name='dispatch_started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='job',
            name='dispatch_error',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='job',
            name='dispatched_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='job',
            name='released_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='job',
            name='failed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
