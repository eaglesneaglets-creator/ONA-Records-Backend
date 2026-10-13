"""
Load PLACEHOLDER catalogue data so the booking flow has something to book.

    python manage.py seed_studio

Idempotent: matches on name and updates in place, so re-running after an
edit here corrects the row rather than duplicating it. Rows added by staff
in admin are never touched.

THESE ARE NOT ONA'S REAL ROOMS, ENGINEERS OR SERVICES. PRODUCT.md is explicit
that placeholder data must not ship as if real. The real catalogue is an
open item with ONA (STATE.md); when it arrives, replace the tables below or
load it through admin and retire this command. Refused under production
settings for exactly that reason.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.studio.models import Engineer, Room, Service

# GHS 200/hr for the live room matches the figure used throughout the
# approved booking-flow design (docs/design/pages/booking-flow.html).
ROOMS = [
    dict(name='Studio A — Live Room', hourly_rate_pesewas=20000, capacity=8, sort_order=10,
         description='Full band tracking room with isolation booth. PLACEHOLDER.'),
    dict(name='Studio B — Vocal Booth', hourly_rate_pesewas=12000, capacity=2, sort_order=20,
         description='Treated vocal and voice-over booth. PLACEHOLDER.'),
    dict(name='Mix Suite', hourly_rate_pesewas=15000, capacity=3, sort_order=30,
         description='Mixing and mastering suite, no live tracking. PLACEHOLDER.'),
]

ENGINEERS = [
    dict(name='Engineer One', specialties=['tracking', 'live band'], sort_order=10,
         bio='PLACEHOLDER — replace with a real ONA engineer.'),
    dict(name='Engineer Two', specialties=['mixing', 'vocal production'], sort_order=20,
         bio='PLACEHOLDER — replace with a real ONA engineer.'),
    dict(name='Engineer Three', specialties=['mastering', 'podcast'], sort_order=30,
         bio='PLACEHOLDER — replace with a real ONA engineer.'),
]

SERVICES = [
    dict(name='Recording session', base_price_pesewas=20000, duration_minutes=60, sort_order=10,
         description='Tracking with an engineer. Priced per hour. PLACEHOLDER.'),
    dict(name='Mixing (per song)', base_price_pesewas=30000, duration_minutes=180, sort_order=20,
         description='Stems in, finished mix out. PLACEHOLDER.'),
    dict(name='Mastering (per song)', base_price_pesewas=15000, duration_minutes=60, sort_order=30,
         description='Final loudness and delivery masters. PLACEHOLDER.'),
    dict(name='Voice-over / podcast', base_price_pesewas=10000, duration_minutes=60, sort_order=40,
         description='Spoken-word recording and clean-up. PLACEHOLDER.'),
]


class Command(BaseCommand):
    help = 'Seed PLACEHOLDER rooms, engineers and services. Refused under production settings.'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true',
                            help='Run on a non-local environment (staging demos only).')

    def handle(self, *args, **options):
        # Keyed on ENVIRONMENT_NAME, not DEBUG: pytest-django forces DEBUG
        # off during tests, and production is the default name when unset
        # — so an unconfigured box refuses, which is the safe failure.
        env = getattr(settings, 'ENVIRONMENT_NAME', 'production')
        if env == 'production':
            raise CommandError('Refusing to load placeholder data on production.')
        if env != 'local' and not options['force']:
            raise CommandError(
                f'Refusing to load placeholder data on {env!r} without --force.'
            )

        with transaction.atomic():
            counts = {
                'rooms': self._upsert(Room, ROOMS),
                'engineers': self._upsert(Engineer, ENGINEERS),
                'services': self._upsert(Service, SERVICES),
            }

        for label, (created, updated) in counts.items():
            self.stdout.write(f'{label}: {created} created, {updated} updated')
        self.stdout.write(self.style.WARNING(
            'PLACEHOLDER catalogue loaded — replace with ONA\'s real data before launch.'
        ))

    @staticmethod
    def _upsert(model, rows):
        created = updated = 0
        for row in rows:
            defaults = {k: v for k, v in row.items() if k != 'name'}
            _, was_created = model.all_objects.update_or_create(name=row['name'], defaults=defaults)
            created += was_created
            updated += not was_created
        return created, updated
