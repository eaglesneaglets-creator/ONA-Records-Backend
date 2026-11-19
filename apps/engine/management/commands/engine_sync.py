"""
Copy ONA's studio calendar into the booking engine, or check that it matches.

    python manage.py engine_sync --dry-run   # what would change; changes nothing
    python manage.py engine_sync             # rooms, engineers, hours, blocks, live bookings

The first run is the import; after that it is the same repair the worker runs
every 30 minutes. Safe to run again at any time.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.engine import client, sync


class Command(BaseCommand):
    help = "Mirror ONA's rooms, engineers, opening hours, blocks and live bookings into the booking engine."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Report what would change; change nothing.')

    def handle(self, *args, dry_run, **opts):
        if not client.is_configured():
            raise CommandError('Set BOOKING_API_URL and BOOKING_API_KEY first.')
        try:
            report = sync.reconcile(dry_run=dry_run)
        except client.BookingEngineUnavailable as exc:
            raise CommandError(f'The booking engine is unreachable: {exc}') from exc
        self.stdout.write(('DRY RUN: nothing changed.\n' if dry_run else '') + 'Booking engine mirror:')
        for part in ('resources', 'closures', 'bookings'):
            counts = ', '.join(f'{v} {k}' for k, v in sorted(report[part].items())) or 'nothing'
            self.stdout.write(f'  {part:<10} {counts}')
        if report['problems']:
            self.stdout.write(self.style.WARNING(f'{len(report["problems"])} problem(s):'))
            for p in report['problems']:
                self.stdout.write(f'  - {p}')
        else:
            self.stdout.write(self.style.SUCCESS('No problems.'))
