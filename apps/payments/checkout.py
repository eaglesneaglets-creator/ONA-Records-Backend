"""
Online payment for a booking, through Paystack (Tasks 8 and 9, ADR-018).

    start_checkout(booking, user)   -> (authorization_url, reference)
    settle(reference)               -> Transaction | None   webhook + return page
    reconcile_pending()             -> counts               Celery beat sweep

How a result reaches us, and why there are three ways:

  1. Paystack's `charge.success` webhook — the normal path. Paystack sends
     webhooks for SUCCESSFUL payments only.
  2. The customer's return page calls settle() with the reference Paystack
     appended to the callback URL — so they see the outcome immediately even
     if the webhook is slow.
  3. reconcile_pending() — a failed or abandoned checkout produces no webhook
     at all, and a webhook can be lost. The sweep asks Paystack about every
     attempt still pending after a few minutes.

All three end in apply_result(), which takes the transaction's row lock
first: the webhook and the return page often arrive in the same second, and
exactly one of them may hold the money, confirm the booking and write the
audit trail. The ledger's one-hold-per-transaction constraint is the
backstop if this code is ever wrong.

Every money decision is made on Paystack's verify response — never on a
webhook body alone, and never because a callback URL was visited.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction as db
from django.utils import timezone

from apps.studio.models import BookingStatus
from core.models import AuditLog

from . import paystack
from .ledger import LedgerError, mark_failed, mark_succeeded, open_transaction
from .manual import outstanding
from .models import Provider, Transaction, TransactionStatus

logger = logging.getLogger(__name__)

# Paystack statuses that mean "not finished yet — ask again later".
IN_FLIGHT = {'abandoned', 'ongoing', 'pending', 'processing', 'queued'}
# The sweep leaves a checkout alone this long: the customer may still be on
# Paystack's page, and the webhook usually lands first anyway.
SWEEP_GRACE = timedelta(minutes=5)
# An attempt still unfinished after this is closed as failed. If the customer
# pays it later after all, settle() still records the money (provider_verified).
ABANDON_AFTER = timedelta(hours=2)
# Older than this, stop asking; staff see it in the console.
SWEEP_HORIZON = timedelta(days=7)


class CheckoutRefused(Exception):
    """This booking cannot be paid for online right now. Message is customer-safe."""


def callback_url() -> str:
    return settings.PAYSTACK_CALLBACK_URL or f'{settings.FRONTEND_URL}/customer/payments/return'


def new_reference(booking) -> str:
    # Ours, unique per attempt, and readable on a Paystack statement:
    # the booking reference plus a random suffix.
    return f'{booking.reference}-{secrets.token_hex(4).upper()}'


def start_checkout(booking, user, request=None) -> tuple[str, str]:
    from apps.studio.models import StudioBooking

    if not paystack.is_configured():
        raise CheckoutRefused('Online payment is not available yet. ONA will be in touch '
                              'about paying for this booking.')
    with db.atomic():
        booking = StudioBooking.objects.select_for_update(of=('self',)).get(pk=booking.pk)
        if booking.status not in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
            raise CheckoutRefused('This booking cannot be paid for — it is '
                                  f'{booking.get_status_display().lower()}.')
        if booking.end_at <= timezone.now():
            raise CheckoutRefused('This session has already ended. Please contact ONA.')
        owed = outstanding(booking)
        if owed <= 0:
            raise CheckoutRefused('This booking is already paid in full.')
        reference = new_reference(booking)
        txn = open_transaction(booking, Provider.PAYSTACK, amount_pesewas=owed,
                               provider_ref=reference)

    # Outside the database transaction: never hold a row lock across an HTTP
    # call to a third party.
    try:
        data = paystack.initialize(
            reference=reference, amount_pesewas=owed, email=user.email,
            callback_url=callback_url(),
            metadata={'booking_reference': booking.reference, 'transaction_id': str(txn.pk)},
        )
    except paystack.PaystackError:
        # The customer never received a checkout link, so nothing can be paid
        # against this attempt.
        mark_failed(txn, 'Checkout could not be started')
        raise
    AuditLog.record('payment.checkout_started', actor=user, target=booking, request=request,
                    reference=booking.reference, provider_ref=reference, amount_pesewas=owed)
    return data['authorization_url'], reference


def settle(reference: str) -> Transaction | None:
    """Ask Paystack about one of our references and record the outcome."""
    txn = (Transaction.objects.filter(provider=Provider.PAYSTACK, provider_ref=reference)
           .select_related('booking').first())
    if txn is None:
        # Not ours (another integration on the same Paystack account), or a
        # forged reference on the return page. Nothing to do.
        logger.warning('Paystack settle: unknown reference %s', reference[:60])
        return None
    if txn.status == TransactionStatus.SUCCEEDED:
        return txn  # already recorded; no API call needed
    return apply_result(txn, paystack.verify(reference))


@db.atomic
def apply_result(txn: Transaction, data: dict) -> Transaction:
    from apps.studio.models import StudioBooking

    txn = Transaction.objects.select_for_update().get(pk=txn.pk)
    if txn.status == TransactionStatus.SUCCEEDED:
        return txn  # the other path (webhook vs return page) got here first

    status = data.get('status')
    booking = txn.booking

    if status == 'success':
        amount, currency = data.get('amount'), data.get('currency')
        if type(amount) is not int or amount != txn.amount_pesewas or currency != 'GHS':
            # Should be impossible — we set the amount. Do not guess: leave it
            # pending and put it in front of a person.
            logger.error('Paystack amount mismatch: ref=%s expected=%s got=%r %r',
                         txn.provider_ref, txn.amount_pesewas, amount, currency)
            AuditLog.record('payment.amount_mismatch', target=booking,
                            reference=booking.reference, provider_ref=txn.provider_ref,
                            expected_pesewas=txn.amount_pesewas, received=amount,
                            currency=currency)
            return txn
        channel = str(data.get('channel') or '')[:40]
        was_failed = txn.status == TransactionStatus.FAILED
        mark_succeeded(txn, note=f'Paystack {channel}'.strip(), provider_verified=True)
        AuditLog.record('payment.succeeded', target=booking, reference=booking.reference,
                        provider='paystack', provider_ref=txn.provider_ref,
                        amount_pesewas=txn.amount_pesewas, channel=channel,
                        after_marked_failed=was_failed)

        booking = StudioBooking.objects.select_for_update(of=('self',)).get(pk=booking.pk)
        if booking.status == BookingStatus.PENDING and outstanding(booking) <= 0 \
                and booking.end_at > timezone.now():
            # Paid in full: the slot is the customer's. The "payment received"
            # email (sent by mark_succeeded, after commit) says so.
            booking.status = BookingStatus.CONFIRMED
            booking.save(update_fields=['status', 'updated_at'])
            AuditLog.record('booking.confirmed', target=booking, reference=booking.reference,
                            previous_status=BookingStatus.PENDING, by_payment=True)
        elif booking.status == BookingStatus.CANCELLED:
            # They paid after cancelling (an old tab). The money is real and is
            # recorded; staff refund it from the console.
            logger.warning('Paystack payment on cancelled booking %s', booking.reference)
            AuditLog.record('payment.received_on_cancelled', target=booking,
                            reference=booking.reference, provider_ref=txn.provider_ref,
                            amount_pesewas=txn.amount_pesewas)
        return Transaction.objects.get(pk=txn.pk)

    if status == 'failed':
        if txn.status == TransactionStatus.PENDING:
            mark_failed(txn, str(data.get('gateway_response') or 'Declined by the provider'))
        return Transaction.objects.get(pk=txn.pk)

    if status == 'reversed':
        # A refund or chargeback made on Paystack's side. Moving money in the
        # ledger is a staff decision; make sure they see it.
        logger.error('Paystack reversed: ref=%s', txn.provider_ref)
        AuditLog.record('payment.reversed', target=booking, reference=booking.reference,
                        provider_ref=txn.provider_ref)
    return txn  # IN_FLIGHT, or a status Paystack adds later: ask again later


def reconcile_pending(now=None) -> dict:
    """Ask Paystack about every online attempt still pending (Celery beat)."""
    now = now or timezone.now()
    counts = {'checked': 0, 'succeeded': 0, 'failed': 0, 'abandoned': 0, 'errors': 0}
    if not paystack.is_configured():
        return counts
    pending = (Transaction.objects
               .filter(provider=Provider.PAYSTACK, status=TransactionStatus.PENDING,
                       created_at__lt=now - SWEEP_GRACE, created_at__gt=now - SWEEP_HORIZON)
               .order_by('created_at')[:200])
    for txn in pending:
        counts['checked'] += 1
        try:
            data = paystack.verify(txn.provider_ref)
            after = apply_result(txn, data)
        except (paystack.PaystackError, LedgerError) as exc:
            counts['errors'] += 1
            logger.warning('Paystack reconcile: ref=%s %s', txn.provider_ref, exc)
            continue
        if after.status == TransactionStatus.SUCCEEDED:
            counts['succeeded'] += 1
        elif after.status == TransactionStatus.FAILED:
            counts['failed'] += 1
        elif data.get('status') in IN_FLIGHT and txn.created_at < now - ABANDON_AFTER:
            mark_failed(after, 'Checkout was not completed')
            counts['abandoned'] += 1
    return counts
