"""
Run the reminder sweep once.

    python manage.py send_reminders

Celery beat runs this every 15 minutes inside the worker. Use this command
from a cron job (e.g. a Railway cron service) where no worker is running —
the sweep is idempotent, so running it more often is harmless.
"""

from django.core.management.base import BaseCommand

from apps.notifications.tasks import send_due_reminders


class Command(BaseCommand):
    help = 'Send session reminders that are due. Safe to run repeatedly.'

    def handle(self, *args, **options):
        queued = send_due_reminders()
        self.stdout.write(f'{queued} reminder(s) queued.')
