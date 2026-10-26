"""
Money ONA received or returned outside the platform, recorded by staff
(Task 16: "manual recording of off-platform payments and refunds, written to
the ledger").

    record_payment(booking, amount, method, reference, note, actor) -> Transaction
    record_refund(txn, amount, note, actor)                         -> LedgerEntry

Cash at the desk, a Mobile Money transfer to ONA's number, a bank transfer:
these go through the same ledger as a Hubtel payment — a Transaction, a HOLD,
a receipt, the customer's "payment received" email — so the books have one
shape. What marks them as manual, and never ambiguous:

  - Transaction.provider = 'manual' ("Recorded by ONA staff") and a `method`
  - every ledger entry carries `created_by` (the staff member) — a provider
    callback leaves it empty — and the staff member's note
  - an AuditLog row with the actor, amount, method and reference

A refund here records money ONA has ALREADY sent back by other means; it
does not move money itself. It may be recorded against any payment, manual
or Hubtel, and can never exceed what is still held.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction as db
from django.db.models import Sum

from apps.studio.models import BookingStatus

from .ledger import LedgerError, balance, mark_succeeded, refund, studio_commission_percent
from .models import EntryType, LedgerEntry, Provider, Transaction, TransactionStatus


def outstanding(booking) -> int:
    """Pesewas still owed: the total, less payments, plus anything refunded."""
    paid = (Transaction.objects.filter(booking=booking, status=TransactionStatus.SUCCEEDED)
            .aggregate(t=Sum('amount_pesewas'))['t'] or 0)
    refunded = (LedgerEntry.objects.filter(transaction__booking=booking,
                                           entry_type=EntryType.REFUND)
                .aggregate(t=Sum('amount_pesewas'))['t'] or 0)
    return booking.total_pesewas - paid + refunded


@db.atomic
def record_payment(booking, amount_pesewas: int, method: str, reference: str, note: str,
                   actor) -> Transaction:
    from apps.studio.models import StudioBooking

    if type(amount_pesewas) is not int or amount_pesewas <= 0:
        raise LedgerError('The amount must be a positive whole number of pesewas.')
    # Serialise payments for one booking: two staff recording the same cash
    # at once must not both pass the "owed" check.
    booking = StudioBooking.objects.select_for_update(of=('self',)).get(pk=booking.pk)
    if booking.status == BookingStatus.CANCELLED:
        raise LedgerError('This booking is cancelled. Reinstate or rebook it before taking payment.')
    owed = outstanding(booking)
    if amount_pesewas > owed:
        raise LedgerError(
            'That is more than is owed on this booking.' if owed > 0
            else 'This booking is already paid in full.')

    ref = reference.strip()
    if ref and Transaction.objects.filter(provider=Provider.MANUAL,
                                          provider_ref__iexact=ref).exists():
        # "MOMO-1" and "momo-1" are the same money (QA F7).
        raise LedgerError('A payment with this reference has already been recorded.')
    try:
        with db.atomic():
            txn = Transaction.objects.create(
                customer=booking.customer, booking=booking, provider=Provider.MANUAL,
                method=method, amount_pesewas=amount_pesewas,
                provider_ref=ref or None,
                commission_percent=studio_commission_percent(),
            )
    except IntegrityError:
        # The (provider, provider_ref) unique constraint: the same Mobile
        # Money or bank reference recorded twice is the same money.
        raise LedgerError('A payment with this reference has already been recorded.')
    mark_succeeded(txn, actor=actor, note=note)
    return txn


@db.atomic
def record_refund(txn: Transaction, amount_pesewas: int, note: str, actor) -> LedgerEntry:
    if txn.status != TransactionStatus.SUCCEEDED:
        raise LedgerError('Only a confirmed payment can be refunded.')
    if not note.strip():
        raise LedgerError('Say how the money was returned (e.g. MoMo reference).')
    return refund(txn, amount_pesewas, actor=actor, note=note.strip())


__all__ = ['balance', 'outstanding', 'record_payment', 'record_refund', 'LedgerError']
