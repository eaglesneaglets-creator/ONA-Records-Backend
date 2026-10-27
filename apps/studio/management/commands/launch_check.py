"""
Pre-launch readiness check (Task 19/20).

    python manage.py launch_check            # on the target environment (Railway shell)

Lists what still stands between this environment and taking real bookings,
as BLOCKER (must fix) or WARN (decide consciously). Exits non-zero when any
blocker remains, so it can gate a release step.

It reads only settings and the live catalogue/FAQ — it changes nothing.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

PLACEHOLDER_MARKERS = ('PLACEHOLDER', 'lorem ipsum', 'replace with a real')


def _has_marker(*texts) -> bool:
    joined = ' '.join(t or '' for t in texts).lower()
    return any(m.lower() in joined for m in PLACEHOLDER_MARKERS)


def collect() -> list[tuple[str, str]]:
    """[(level, message)] - level is 'BLOCKER' or 'WARN'."""
    from apps.content.models import Faq
    from apps.studio.models import Engineer, Package, Room, Service

    out: list[tuple[str, str]] = []

    # ── Content ─────────────────────────────────────────────────────────
    rooms = list(Room.objects.active())
    if not rooms:
        out.append(('BLOCKER', 'No rooms are on sale - customers have nothing to book.'))
    for kind, items, fields in (
        ('room', rooms, ('name', 'description')),
        ('engineer', list(Engineer.objects.active()), ('name', 'bio')),
        ('service', list(Service.objects.active()), ('name', 'description')),
        ('package', list(Package.objects.active()), ('name', 'description')),
    ):
        for item in items:
            if _has_marker(*(getattr(item, f) for f in fields)):
                out.append(('BLOCKER', f'Placeholder text on {kind} "{item.name}" - replace it in '
                                       'Studio & prices, or switch it off.'))
    if not Engineer.objects.active().exists():
        out.append(('BLOCKER', 'No engineers are on sale - a booking needs one.'))
    for faq in Faq.objects.filter(is_published=True):
        if _has_marker(faq.question, faq.answer):
            out.append(('BLOCKER', f'Placeholder text in the FAQ "{faq.question}".'))

    # ── Settings ────────────────────────────────────────────────────────
    if settings.DEBUG:
        out.append(('BLOCKER', 'DEBUG is on.'))
    if settings.ADMIN_URL.strip('/') == 'admin':
        out.append(('BLOCKER', 'ADMIN_URL is the default "admin/" - set a private path.'))
    if 'console' in settings.EMAIL_BACKEND or 'locmem' in settings.EMAIL_BACKEND:
        out.append(('BLOCKER', f'Email is not sent ({settings.EMAIL_BACKEND}). Customers would '
                               'never receive confirmations or verification links.'))
    if not getattr(settings, 'BOOKING_REQUIRES_VERIFIED_EMAIL', True):
        out.append(('BLOCKER', 'BOOKING_REQUIRES_VERIFIED_EMAIL is off.'))
    if not getattr(settings, 'CELERY_BROKER_URL', ''):
        out.append(('WARN', 'No Celery broker: emails send inline and session reminders do not '
                            'run on a schedule. Add Redis and a worker with CELERY_BEAT=1.'))
    if not all(getattr(settings, k, '') for k in ('HUBTEL_API_KEY', 'HUBTEL_API_SECRET',
                                                    'HUBTEL_MERCHANT_ID')):
        out.append(('WARN', 'Hubtel credentials are not set: no online payment. Staff can still '
                            'record payments by hand.'))
    if getattr(settings, 'STUDIO_COMMISSION_PERCENT', 0) == 0:
        out.append(('WARN', 'STUDIO_COMMISSION_PERCENT is 0 - fine if ONA takes no commission on '
                            'its own sessions (OQ3); otherwise set it before the first payment.'))
    return out


class Command(BaseCommand):
    help = 'List what still blocks taking real bookings on this environment.'

    def handle(self, *args, **options):
        findings = collect()
        blockers = [m for lvl, m in findings if lvl == 'BLOCKER']
        for lvl, msg in findings:
            style = self.style.ERROR if lvl == 'BLOCKER' else self.style.WARNING
            self.stdout.write(style(f'{lvl:8} {msg}'))
        if blockers:
            raise CommandError(f'{len(blockers)} blocker(s) - not ready to launch.')
        self.stdout.write(self.style.SUCCESS(
            'Ready: no blockers.' + (' Review the warnings above.' if findings else '')))
