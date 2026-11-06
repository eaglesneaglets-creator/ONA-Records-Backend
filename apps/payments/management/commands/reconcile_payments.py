"""
Settle online payments now, by hand — for a stuck payment (docs/runbook.md).

    python manage.py reconcile_payments                     # every pending Paystack attempt
    python manage.py reconcile_payments --reference ONA-1A2B3C-9F8E7D6C
    python manage.py reconcile_payments --release           # also release unpaid holds

The same code the Celery beat sweeps run every 10 and 5 minutes; this is for
when the worker is down, or a customer is on the phone. Every outcome is
decided by Paystack's verify endpoint, exactly as the webhook would.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.payments import paystack
from apps.payments.checkout import reconcile_pending, release_unpaid, settle


class Command(BaseCommand):
    help = 'Ask Paystack about pending payments and record the outcome.'

    def add_arguments(self, parser):
        parser.add_argument('--reference', help='One Paystack reference (from the console).')
        parser.add_argument('--release', action='store_true',
                            help='Also release unpaid bookings past the hold.')

    def handle(self, *args, reference=None, release=False, **options):
        if not paystack.is_configured():
            raise CommandError('PAYSTACK_SECRET_KEY is not set on this environment.')
        if reference:
            try:
                txn = settle(reference)
            except paystack.PaystackError as exc:
                raise CommandError(f'Paystack could not be asked: {exc}')
            if txn is None:
                raise CommandError(f'No Paystack payment with reference {reference}.')
            self.stdout.write(f'{reference}: {txn.status} · {txn.amount_display}'
                              + (f' · {txn.failure_reason}' if txn.failure_reason else '')
                              + f' · booking {txn.booking.reference} is {txn.booking.status}')
            return
        counts = reconcile_pending()
        self.stdout.write('Pending payments: ' + ', '.join(f'{k} {v}' for k, v in counts.items()))
        if release:
            self.stdout.write(f'Unpaid bookings released: {release_unpaid()}')
