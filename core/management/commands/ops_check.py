"""
Is this environment actually wired up? (Task 20 — "monitoring verified")

    python manage.py ops_check                 # database, cache, Celery workers, Sentry config
    python manage.py ops_check --sentry-test   # also send one test event to Sentry

launch_check asks "is the CONTENT ready?"; this asks "are the MOVING PARTS
reachable from here?" — each answered by actually using it, not by reading a
setting. Exits non-zero when something required is down, so it can gate a
release step.
"""

import uuid

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = 'Check the database, cache, Celery workers and Sentry are reachable.'

    def add_arguments(self, parser):
        parser.add_argument('--sentry-test', action='store_true',
                            help='Send a test message to Sentry (check it arrives).')

    def handle(self, *args, sentry_test=False, **options):
        failures = []

        def report(ok: bool, what: str, detail: str = '', required: bool = True):
            mark = self.style.SUCCESS('ok  ') if ok else (
                self.style.ERROR('FAIL') if required else self.style.WARNING('warn'))
            self.stdout.write(f'{mark} {what}' + (f' - {detail}' if detail else ''))
            if not ok and required:
                failures.append(what)

        try:
            with connection.cursor() as cur:
                cur.execute('SELECT 1')
            report(True, 'database', connection.settings_dict.get('HOST') or 'local')
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            report(False, 'database', str(exc)[:120])

        key, token = f'ops-check:{uuid.uuid4()}', uuid.uuid4().hex
        try:
            cache.set(key, token, 30)
            shared = 'redis' in settings.CACHES['default']['BACKEND'].lower()
            report(cache.get(key) == token, 'cache',
                   'Redis (shared by all workers)' if shared
                   else 'per-process only: lockouts and the refresh grace (D8) are not '
                        'shared between workers - add REDIS_URL', required=False)
            if not shared:
                self.stdout.write('     (cache works, but is not shared)')
            cache.delete(key)
        except Exception as exc:  # noqa: BLE001
            report(False, 'cache', str(exc)[:120])

        broker = getattr(settings, 'CELERY_BROKER_URL', '')
        if not broker:
            report(False, 'celery', 'no broker configured: emails send inline, reminders, '
                                    'the Paystack sweep and the 1-hour release do not run',
                   required=False)
        else:
            try:
                from config.celery import app
                replies = app.control.ping(timeout=3.0) or []
                report(bool(replies), 'celery workers',
                       f'{len(replies)} answering' if replies else 'broker reachable, but no '
                       'worker answered - is the worker service running?')
            except Exception as exc:  # noqa: BLE001
                report(False, 'celery broker', str(exc)[:120])

        dsn = getattr(settings, 'SENTRY_DSN', '')
        report(bool(dsn), 'sentry', 'configured' if dsn else 'SENTRY_DSN not set: errors are '
                                                            'only in the logs', required=False)
        if sentry_test and dsn:
            import sentry_sdk
            event_id = sentry_sdk.capture_message(
                f'ops_check test event from {getattr(settings, "ENVIRONMENT_NAME", "production")}')
            sentry_sdk.flush(timeout=5)
            self.stdout.write(f'     test event sent: {event_id} - look for it in Sentry')

        if failures:
            raise CommandError(f'{len(failures)} required check(s) failed: {", ".join(failures)}')
        self.stdout.write(self.style.SUCCESS('Required parts reachable.'))
