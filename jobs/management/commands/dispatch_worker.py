import logging
import time

from django.core.management.base import BaseCommand

from jobs.bambuddy import BambuddyError, BambuddyNotConfigured
from jobs.dispatch import (
    claim_next_dispatch,
    dispatch_claimed_job,
    reset_stale_dispatch_claims,
    sync_from_bambuddy,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Send sliced jobs to Bambuddy and poll it for print status (a fallback '
        'for missed webhooks). Run as a service (see README "Dispatch worker").'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval', type=float, default=5.0,
            help='Seconds to sleep when there is nothing to dispatch (default 5).',
        )
        parser.add_argument(
            '--poll-interval', type=float, default=60.0,
            help='Seconds between status polls of Bambuddy (default 60, 0 disables).',
        )
        parser.add_argument(
            '--once', action='store_true',
            help='Dispatch everything currently waiting, poll once, then exit.',
        )

    def handle(self, *args, interval, poll_interval, once, **options):
        logger.info('Dispatch worker started.')
        last_poll = 0.0
        try:
            while True:
                reset_stale_dispatch_claims()
                job = claim_next_dispatch()
                if job is not None:
                    dispatch_claimed_job(job)
                    continue
                if poll_interval and (once or time.monotonic() - last_poll >= poll_interval):
                    last_poll = time.monotonic()
                    self._poll()
                if once:
                    return
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info('Dispatch worker stopped.')

    def _poll(self):
        try:
            changed = sync_from_bambuddy()
        except (BambuddyError, BambuddyNotConfigured) as exc:
            logger.warning('Bambuddy status poll failed: %s', exc)
            return
        if changed:
            logger.info('Status poll updated %s job(s).', changed)
