from django.core.management.base import BaseCommand, CommandError

from jobs.models import PrinterAgent


class Command(BaseCommand):
    help = (
        'Create a print agent (or give an existing one a new key) and print its '
        'key. The key is stored hashed and is only shown here, once.'
    )

    def add_arguments(self, parser):
        parser.add_argument('name', help='Agent name, e.g. "school-pi".')
        parser.add_argument(
            '--rotate', action='store_true',
            help='Replace the key of an existing agent (the old key stops working).',
        )

    def handle(self, *args, name, rotate, **options):
        agent = PrinterAgent.objects.filter(name=name).first()
        if agent is not None and not rotate:
            raise CommandError(f'Agent "{name}" exists. Use --rotate to give it a new key.')
        if agent is None:
            if rotate:
                raise CommandError(f'No agent named "{name}".')
            agent = PrinterAgent(name=name)
        key = agent.set_new_key()
        agent.save()
        self.stdout.write(f'Agent "{name}" key (put it in JOHNCAD_AGENT_KEY; shown once):')
        self.stdout.write(key)
