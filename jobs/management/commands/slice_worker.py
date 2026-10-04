import logging
import time

from django.core.management.base import BaseCommand

from jobs.services import claim_next_job, reset_stale_claims, slice_claimed_job

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Slice approved jobs in the background. Run as a service on the same host '
        'as OrcaSlicer (see README "Slicing worker").'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval', type=float, default=5.0,
            help='Seconds to sleep when there is nothing to slice (default 5).',
        )
        parser.add_argument(
            '--once', action='store_true',
            help='Slice everything currently waiting, then exit.',
        )

    def handle(self, *args, interval, once, **options):
        logger.info('Slice worker started.')
        try:
            while True:
                reset_stale_claims()
                job = claim_next_job()
                if job is not None:
                    slice_claimed_job(job)
                    continue
                if once:
                    return
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info('Slice worker stopped.')
