"""
Load ONA's real catalogue (their answers, November 2026) and switch the
placeholders off.

    python manage.py load_ona_catalogue            # on staging / production
    python manage.py load_ona_catalogue --dry-run  # say what would change

Idempotent: items are matched by name and updated in place, so running it
twice changes nothing the second time. Everything else in the catalogue —
the placeholder rooms, engineers, services and packages — is switched OFF,
not deleted: old bookings still point at them, and staff can switch one back
on from Studio & prices.

After this, staff own the catalogue: edit prices and wording in the console,
not here. Local development keeps `seed_studio` (the E2E suite books its
placeholder names).

Copy is ONA's own, lightly tidied ("3years +" -> "3+ years").
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.studio.models import Engineer, Package, PriceUnit, Room, Service

GHS = 100  # pesewas

ROOM = {
    'name': 'Studio One',
    'description': (
        'Studio One is a state-of-the-art creative hub equipped with flexible acoustics, '
        'dynamic lighting, and professional recording gear. Built for maximum versatility, it '
        'offers an ideal environment for capturing high-quality content ranging from intimate '
        'podcasts to full-scale livestreams and performances.'),
    'capacity': 30,
    # "Flexible pricing based on service (Engineer included)": no room hire.
    'hourly_rate_pesewas': None,
}

ENGINEER = {
    'name': 'Michael Kontor',
    'specialties': ['Audio Recording', 'Mixing', 'Photography & Videography'],
    'bio': (
        'Michael is an audio engineer and visual storyteller specializing in tracking, mixing, '
        'and vocal/voiceover production for music and digital media. Bringing over 1 year of '
        'hands-on experience in sound engineering and 3+ years in visual production, he '
        'seamlessly bridges the gap between high-fidelity audio and crisp '
        'photography/cinematography. Whether shaping raw tracks into polished mixes or '
        'capturing dynamic video, Michael ensures your project leaves a lasting creative '
        'impact.'),
}

RENTAL, AUDIO, VIDEO = 'Space & Studio Rental', 'Audio & Digital Production', \
    'Video & Content Production'

# (category, name, GHS, unit, typical minutes, description)
SERVICES = [
    (RENTAL, 'Rehearsal Space', 150, PriceUnit.HOUR, 60,
     'Access to fully treated studio space and equipment for band or solo rehearsals.'),
    (RENTAL, 'Video Shoot Location', 300, PriceUnit.HOUR, 60,
     'Access to studio space equipped with essential video lighting.'),
    (RENTAL, 'Full Day Studio Pass', 1000, PriceUnit.DAY, 480,
     'Full-day access to studio space and equipment for video shoots or extended rehearsals.'),
    (AUDIO, 'Digital Production', 4000, PriceUnit.SONG, 60,
     'Complete digital composition, beat crafting, arrangement, and sound design to bring '
     'your track to life from scratch.'),
    (AUDIO, 'Multitrack Live Recording', 400, PriceUnit.HOUR, 60,
     'Multi-channel recording for full bands and vocal ensembles. Includes raw, unprocessed '
     'multi-track data delivered via email or external drive.'),
    (AUDIO, 'Overdub & Solo Recording', 300, PriceUnit.HOUR, 60,
     'Focused recording sessions for individual instruments, drums, or vocal overdubs. '
     'Delivered as raw audio files via email or drive.'),
    (AUDIO, 'Mixing & Mastering', 1000, PriceUnit.TRACK, 60,
     'Professional post-production for songs up to 8 minutes long. Includes up to 3 free '
     'revisions. Tracks over 8 minutes add GH₵ 50 per extra minute, agreed with ONA.'),
    (VIDEO, 'Music Video Shoot (Miming)', 4000, PriceUnit.SONG, 60,
     'Professional 2-camera setup for tracks under 8 minutes. Includes complete video '
     'editing, color grading, and final delivery (includes up to 3 free revisions).'),
    (VIDEO, 'Live Session Recording & Video', 5000, PriceUnit.SONG, 60,
     'Premium 3-camera live performance video and audio capture for tracks under 8 minutes. '
     'Complete with full audio recording, video editing, and final delivery (includes up to '
     '3 free revisions).'),
    (VIDEO, 'Social Media Content Creation', 600, PriceUnit.HOUR, 60,
     'Tailored for short-form video content (up to 90 seconds). Includes studio session '
     'time, audio setup (vocal + 1 instrument), camera capture, and editing for 3 final '
     'video clips (includes up to 3 free revisions).'),
]


class Command(BaseCommand):
    help = "Load ONA's real rooms, engineers and services; switch placeholders off."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, dry_run=False, **options):
        with transaction.atomic():
            log = self._load()
            for line in log:
                self.stdout.write(line)
            if dry_run:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING('Dry run: nothing saved.'))
            else:
                self.stdout.write(self.style.SUCCESS('ONA catalogue loaded.'))

    def _load(self) -> list[str]:
        log = []

        def upsert(model, name, **fields):
            obj = model.objects.filter(name=name).first()  # soft-deleted rows excluded
            created = obj is None
            obj = obj or model(name=name)
            changed = created
            for k, v in {**fields, 'is_active': True}.items():
                if getattr(obj, k) != v:
                    setattr(obj, k, v)
                    changed = True
            if changed:
                obj.save()
            log.append(f'{"+" if created else ("~" if changed else "=")} {model.__name__}: {name}')
            return obj

        room = upsert(Room, ROOM['name'], **{k: v for k, v in ROOM.items() if k != 'name'},
                      sort_order=0)
        eng = upsert(Engineer, ENGINEER['name'],
                     **{k: v for k, v in ENGINEER.items() if k != 'name'}, sort_order=0)
        keep_services = []
        for i, (cat, name, ghs, unit, minutes, desc) in enumerate(SERVICES):
            keep_services.append(upsert(
                Service, name, category=cat, base_price_pesewas=ghs * GHS, price_unit=unit,
                duration_minutes=minutes, description=desc, sort_order=i).pk)

        for model, keep in ((Room, [room.pk]), (Engineer, [eng.pk]),
                            (Service, keep_services), (Package, [])):
            off = model.objects.filter(is_active=True).exclude(pk__in=keep)
            for obj in off:
                log.append(f'- {model.__name__}: {obj.name} (switched off)')
            off.update(is_active=False)
        return log
