from django.core.management.base import BaseCommand

from jobs.models import Job
from jobs.services import discard_gcode


class Command(BaseCommand):
    help = (
        'Delete leftover G-code files from finished and rejected jobs '
        '(normally removed when a job finishes; this catches failed deletes '
        'and jobs finished before that was added).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='List what would be deleted without deleting anything.',
        )

    def handle(self, *args, dry_run, **options):
        jobs = Job.objects.filter(
            status__in=[Job.Status.FINISHED, Job.Status.REJECTED],
        ).exclude(gcode='')
        removed = failed = 0
        for job in jobs:
            if dry_run:
                self.stdout.write(f'Would delete {job.gcode.name} (job #{job.id})')
                removed += 1
            elif discard_gcode(job):
                removed += 1
            else:
                failed += 1
        verb = 'Would delete' if dry_run else 'Deleted'
        self.stdout.write(f'{verb} G-code for {removed} job(s).')
        if failed:
            self.stderr.write(f'{failed} file(s) could not be deleted; see the log.')
