"""
Online payment for a booking, through Paystack (Tasks 8 and 9, ADR-018),
on ONA's terms (their answers, November 2026):

  * a 50% DEPOSIT secures the booking (BOOKING_DEPOSIT_PERCENT); the balance
    is paid before or on the day. A customer may also pay in full at once.
  * Paystack's processing fee is ADDED to what the customer pays, itemised
    before they pay (PAYSTACK_PASS_FEE_TO_CUSTOMER, PAYSTACK_FEE_BASIS_POINTS).
  * an unpaid booking holds its slot for 1 HOUR (UNPAID_HOLD_MINUTES), then
    is released back to the calendar.

    payment_options(booking)        -> [PaymentOption]       what "Pay" offers
    start_checkout(booking, user, pay) -> (authorization_url, reference)
    settle(reference)               -> Transaction | None    webhook + return page
    reconcile_pending()             -> counts                Celery beat sweep
    release_unpaid()                -> count                 Celery beat sweep

How a result reaches us, and why there are three ways:

  1. Paystack's `charge.success` webhook — the normal path. Paystack sends
     webhooks for SUCCESSFUL payments only.
  2. The customer's return page calls settle() with the reference Paystack
     appended to the callback URL.
  3. reconcile_pending() — a failed or abandoned checkout produces no webhook
     at all, and a webhook can be lost.

All three end in apply_result(), which takes the transaction's row lock
first, so exactly one of them holds the money, confirms the booking and
writes the audit trail. The ledger's one-hold-per-transaction constraint is
the backstop if this code is ever wrong.

THE FEE IS NOT BOOKING MONEY. Transaction.amount_pesewas is what goes towards
the booking — what the ledger holds, what the receipt says was received, what
"paid" sums. Transaction.fee_pesewas is charged on top and kept by Paystack.
Paystack is asked for, and must confirm, amount + fee.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction as db
from django.utils import timezone

from apps.studio.models import BookingStatus
from core.models import AuditLog

from . import paystack
from .ledger import LedgerError, mark_failed, mark_succeeded, open_transaction
from .manual import outstanding
from .money import deposit_pesewas, format_ghs, pass_on_fee
from .models import Provider, Transaction, TransactionStatus

logger = logging.getLogger(__name__)

# Paystack statuses that mean "not finished yet — ask again later".
IN_FLIGHT = {'abandoned', 'ongoing', 'pending', 'processing', 'queued'}
SWEEP_GRACE = timedelta(minutes=5)
ABANDON_AFTER = timedelta(hours=2)
SWEEP_HORIZON = timedelta(days=7)
# On a pending payment Paystack confirmed for a different amount: a person
# decides (docs/runbook.md §4). The sweep leaves these alone.
MISMATCH = 'Amount does not match — check Paystack before recording anything'
# An unpaid booking whose customer is on Paystack's page right now is not
# released under them; their checkout gets this long.
CHECKOUT_GRACE = timedelta(minutes=20)


class CheckoutRefused(Exception):
    """This booking cannot be paid for online right now. Message is customer-safe."""


# ---------------------------------------------------------------------------
# What there is to pay
# ---------------------------------------------------------------------------

def deposit_for(booking) -> int:
    return deposit_pesewas(booking.total_pesewas, settings.BOOKING_DEPOSIT_PERCENT)


def fee_for(amount_pesewas: int) -> int:
    if not settings.PAYSTACK_PASS_FEE_TO_CUSTOMER:
        return 0
    return pass_on_fee(amount_pesewas, settings.PAYSTACK_FEE_BASIS_POINTS)


@dataclass(frozen=True)
class PaymentOption:
    kind: str               # 'deposit' | 'full' | 'balance'
    label: str
    amount_pesewas: int     # towards the booking
    fee_pesewas: int        # processing fee on top

    @property
    def charge_pesewas(self) -> int:
        return self.amount_pesewas + self.fee_pesewas

    def as_dict(self) -> dict:
        return {
            'kind': self.kind, 'label': self.label,
            'amount_pesewas': self.amount_pesewas, 'amount_display': format_ghs(self.amount_pesewas),
            'fee_pesewas': self.fee_pesewas, 'fee_display': format_ghs(self.fee_pesewas),
            'charge_pesewas': self.charge_pesewas, 'charge_display': format_ghs(self.charge_pesewas),
        }


def payment_options(booking) -> list[PaymentOption]:
    """
    Nothing paid yet: the deposit, or everything. Deposit (or more) paid: the
    balance. Fully paid: nothing. Paid figures are net of refunds.
    """
    from apps.studio.money import net_paid

    owed = outstanding(booking)
    if owed <= 0:
        return []
    due_now = deposit_for(booking) - net_paid(booking)
    if 0 < due_now < owed:
        return [PaymentOption('deposit', f'{settings.BOOKING_DEPOSIT_PERCENT}% deposit',
                              due_now, fee_for(due_now)),
                PaymentOption('full', 'Pay in full', owed, fee_for(owed))]
    kind, label = ('balance', 'Balance') if net_paid(booking) > 0 else ('full', 'Pay in full')
    return [PaymentOption(kind, label, owed, fee_for(owed))]


# ---------------------------------------------------------------------------
# Starting a checkout
# ---------------------------------------------------------------------------

def callback_url() -> str:
    return settings.PAYSTACK_CALLBACK_URL or f'{settings.FRONTEND_URL}/customer/payments/return'


def new_reference(booking) -> str:
    return f'{booking.reference}-{secrets.token_hex(4).upper()}'


def start_checkout(booking, user, pay: str = 'deposit', request=None) -> tuple[str, str]:
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
        options = {o.kind: o for o in payment_options(booking)}
        if not options:
            raise CheckoutRefused('This booking is already paid in full.')
        # Asked for the deposit when it is already paid: charge the balance.
        # Asked for "full": whatever is still owed.
        if pay == 'deposit':
            option = options.get('deposit') or next(iter(options.values()))
        else:
            option = options.get('full') or options.get('balance') \
                or next(iter(options.values()))
        reference = new_reference(booking)
        txn = open_transaction(booking, Provider.PAYSTACK, amount_pesewas=option.amount_pesewas,
                               provider_ref=reference)
        if option.fee_pesewas:
            Transaction.objects.filter(pk=txn.pk).update(fee_pesewas=option.fee_pesewas)
            txn.fee_pesewas = option.fee_pesewas

    # Outside the database transaction: never hold a row lock across an HTTP
    # call to a third party.
    try:
        data = paystack.initialize(
            reference=reference, amount_pesewas=option.charge_pesewas, email=user.email,
            callback_url=callback_url(), channels=settings.PAYSTACK_CHANNELS,
            metadata={'booking_reference': booking.reference, 'transaction_id': str(txn.pk),
                      'payment': option.kind},
        )
    except paystack.PaystackError:
        mark_failed(txn, 'Checkout could not be started')
        raise
    AuditLog.record('payment.checkout_started', actor=user, target=booking, request=request,
                    reference=booking.reference, provider_ref=reference, payment=option.kind,
                    amount_pesewas=option.amount_pesewas, fee_pesewas=option.fee_pesewas)
    return data['authorization_url'], reference


# ---------------------------------------------------------------------------
# Settling
# ---------------------------------------------------------------------------

def settle(reference: str) -> Transaction | None:
    """Ask Paystack about one of our references and record the outcome."""
    txn = (Transaction.objects.filter(provider=Provider.PAYSTACK, provider_ref=reference)
           .select_related('booking').first())
    if txn is None:
        logger.warning('Paystack settle: unknown reference %s', reference[:60])
        return None
    if txn.status == TransactionStatus.SUCCEEDED:
        return txn
    return apply_result(txn, paystack.verify(reference))


@db.atomic
def apply_result(txn: Transaction, data: dict) -> Transaction:
    txn = Transaction.objects.select_for_update().get(pk=txn.pk)
    if txn.status == TransactionStatus.SUCCEEDED:
        return txn  # the other path (webhook vs return page) got here first

    status = data.get('status')
    booking = txn.booking

    if status == 'success':
        amount, currency = data.get('amount'), data.get('currency')
        expected = txn.amount_pesewas + txn.fee_pesewas
        if type(amount) is not int or amount != expected or currency != 'GHS':
            if txn.failure_reason != MISMATCH:
                logger.error('Paystack amount mismatch: ref=%s expected=%s got=%r %r',
                             txn.provider_ref, expected, amount, currency)
                AuditLog.record('payment.amount_mismatch', target=booking,
                                reference=booking.reference, provider_ref=txn.provider_ref,
                                expected_pesewas=expected, received=amount, currency=currency)
                # Still pending — money may have moved — but marked, so staff
                # see it and the sweep stops asking.
                Transaction.objects.filter(pk=txn.pk).update(failure_reason=MISMATCH)
            return Transaction.objects.get(pk=txn.pk)
        channel = str(data.get('channel') or '')[:40]
        was_failed = txn.status == TransactionStatus.FAILED
        mark_succeeded(txn, note=f'Paystack {channel}'.strip(), provider_verified=True)
        AuditLog.record('payment.succeeded', target=booking, reference=booking.reference,
                        provider='paystack', provider_ref=txn.provider_ref,
                        amount_pesewas=txn.amount_pesewas, fee_pesewas=txn.fee_pesewas,
                        channel=channel, after_marked_failed=was_failed)
        _after_payment(booking, txn)
        return Transaction.objects.get(pk=txn.pk)

    if status == 'failed':
        if txn.status == TransactionStatus.PENDING:
            mark_failed(txn, str(data.get('gateway_response') or 'Declined by the provider'))
        return Transaction.objects.get(pk=txn.pk)

    if status == 'reversed':
        logger.error('Paystack reversed: ref=%s', txn.provider_ref)
        AuditLog.record('payment.reversed', target=booking, reference=booking.reference,
                        provider_ref=txn.provider_ref)
    return txn


def _after_payment(booking, txn) -> None:
    """Confirm on the deposit; bring back a booking released moments before."""
    from apps.studio.models import StudioBooking
    from apps.studio.money import net_paid

    booking = StudioBooking.objects.select_for_update(of=('self',)).get(pk=booking.pk)
    secured = net_paid(booking) >= deposit_for(booking)

    if booking.status == BookingStatus.CANCELLED and _was_released_unpaid(booking) \
            and secured and booking.end_at > timezone.now():
        # Paid just after the 1-hour hold ran out (an old tab). If the slot is
        # still free, give it back; the exclusion constraint decides.
        try:
            with db.atomic():
                booking.status = BookingStatus.CONFIRMED
                booking.save(update_fields=['status', 'updated_at'])
        except IntegrityError:
            booking.refresh_from_db()
        else:
            AuditLog.record('booking.reinstated', target=booking, reference=booking.reference,
                            provider_ref=txn.provider_ref, by_payment=True)
            return

    if booking.status == BookingStatus.PENDING and secured and booking.end_at > timezone.now():
        # The deposit is in: the slot is the customer's. The "payment
        # received" email (sent by mark_succeeded, after commit) says so.
        booking.status = BookingStatus.CONFIRMED
        booking.save(update_fields=['status', 'updated_at'])
        AuditLog.record('booking.confirmed', target=booking, reference=booking.reference,
                        previous_status=BookingStatus.PENDING, by_payment=True)
    elif booking.status == BookingStatus.CANCELLED:
        logger.warning('Paystack payment on cancelled booking %s', booking.reference)
        AuditLog.record('payment.received_on_cancelled', target=booking,
                        reference=booking.reference, provider_ref=txn.provider_ref,
                        amount_pesewas=txn.amount_pesewas)


def _was_released_unpaid(booking) -> bool:
    return AuditLog.objects.filter(target_type='studio.studiobooking', target_id=str(booking.pk),
                                   action='booking.released_unpaid').exists()


# ---------------------------------------------------------------------------
# Sweeps (Celery beat)
# ---------------------------------------------------------------------------

def reconcile_pending(now=None) -> dict:
    """Ask Paystack about every online attempt still pending."""
    now = now or timezone.now()
    counts = {'checked': 0, 'succeeded': 0, 'failed': 0, 'abandoned': 0, 'errors': 0}
    if not paystack.is_configured():
        return counts
    pending = (Transaction.objects
               .filter(provider=Provider.PAYSTACK, status=TransactionStatus.PENDING,
                       created_at__lt=now - SWEEP_GRACE, created_at__gt=now - SWEEP_HORIZON)
               .exclude(failure_reason=MISMATCH)
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


def release_unpaid(now=None) -> int:
    """
    Release self-service bookings left unpaid past the hold (ONA: "If payment
    is not completed within 1 hour ... your time slot will be automatically
    released back to the public calendar").

    Only while online payment is switched on — without it a customer has no
    way to pay within the hour, and staff confirm bookings by hand. Bookings
    staff made for a customer are theirs to manage and never released here.
    """
    from apps.notifications.dispatch import notify_booking
    from apps.notifications.models import Kind
    from apps.studio.models import StudioBooking
    from apps.studio.money import with_paid

    if not paystack.is_configured() or not settings.UNPAID_HOLD_MINUTES:
        return 0
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=settings.UNPAID_HOLD_MINUTES)
    candidates = with_paid(StudioBooking.objects.filter(
        status=BookingStatus.PENDING, created_by__isnull=True, created_at__lt=cutoff))
    released = 0
    for b in candidates:
        if b.paid_pesewas - b.refunded_pesewas > 0:
            continue
        if Transaction.objects.filter(booking=b, provider=Provider.PAYSTACK,
                                      status=TransactionStatus.PENDING,
                                      created_at__gt=now - CHECKOUT_GRACE).exists():
            continue  # they are paying right now
        with db.atomic():
            locked = StudioBooking.objects.select_for_update(of=('self',)).get(pk=b.pk)
            if locked.status != BookingStatus.PENDING:
                continue
            locked.status = BookingStatus.CANCELLED
            locked.save(update_fields=['status', 'updated_at'])
            AuditLog.record('booking.released_unpaid', target=locked, reference=locked.reference,
                            hold_minutes=settings.UNPAID_HOLD_MINUTES)
            notify_booking(locked, Kind.BOOKING_CANCELLED)
        released += 1
    return released
