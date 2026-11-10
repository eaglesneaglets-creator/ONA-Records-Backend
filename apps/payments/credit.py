"""
Deposit credit — ONA's terms: "If you cancel at least 48 hours before your
session, your deposit will not be lost — it will be applied as credit toward
a rescheduled session", booked and completed within 30 days of the
cancellation.

    available_credits(customer)            -> [Credit]
    apply_credit(source, target, actor)    -> Transaction

Where credit comes from: a cancellation whose audit entry carries
`credit_until` — written when a customer cancels a paid booking (they can only
do that with 48 hours' notice), or when staff cancel with "keep as credit".

How it moves, in the append-only ledger: the money stays with ONA throughout.
On the cancelled booking, a TRANSFER entry takes it out of what that booking
holds (not a refund — nothing was returned). On the new booking, a payment
from provider "credit" holds it, with a receipt like any other payment. Both
ends, the staff member and the amount are in the audit trail.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date

from django.db import transaction as db
from django.utils import timezone

from core.models import AuditLog

from .ledger import LedgerError, balance, mark_succeeded, open_transaction
from .manual import outstanding
from .models import Beneficiary, EntryType, LedgerEntry, Provider, Transaction, TransactionStatus


@dataclass(frozen=True)
class Credit:
    booking_id: str
    reference: str
    amount_pesewas: int
    until: date

    def as_dict(self) -> dict:
        from .money import format_ghs
        return {'booking': self.booking_id, 'reference': self.reference,
                'amount_pesewas': self.amount_pesewas,
                'amount_display': format_ghs(self.amount_pesewas),
                'until': self.until.isoformat()}


def _credit_until(booking) -> date | None:
    entry = (AuditLog.objects.filter(target_type='studio.studiobooking', target_id=str(booking.pk),
                                     action='booking.cancelled')
             .order_by('-created_at').first())
    raw = (entry.metadata or {}).get('credit_until') if entry else None
    return date.fromisoformat(raw) if raw else None


def available_credits(customer, today: date | None = None) -> list[Credit]:
    """Cancelled bookings of this customer whose credit is unexpired and unused."""
    from apps.studio.models import BookingStatus, StudioBooking
    from apps.studio.money import net_paid

    today = today or timezone.localdate()
    out = []
    for b in StudioBooking.objects.filter(customer=customer, status=BookingStatus.CANCELLED):
        held = net_paid(b)
        until = _credit_until(b) if held > 0 else None
        if until and until >= today:
            out.append(Credit(str(b.pk), b.reference, held, until))
    return sorted(out, key=lambda c: c.until)


@db.atomic
def apply_credit(source, target, actor, request=None) -> Transaction:
    from apps.studio.models import BookingStatus, StudioBooking

    if source.pk == target.pk:
        raise LedgerError('Choose a different booking to apply the credit to.')
    # Lock both bookings in a fixed order, so two staff applying the same
    # credit at once cannot both spend it.
    first, second = sorted([source.pk, target.pk], key=str)
    locked = {b.pk: b for b in StudioBooking.objects.select_for_update(of=('self',))
              .filter(pk__in=[first, second]).order_by('pk')}
    source, target = locked[source.pk], locked[target.pk]

    if source.customer_id != target.customer_id:
        raise LedgerError('Credit can only move between bookings of the same customer.')
    if source.status != BookingStatus.CANCELLED:
        raise LedgerError('Credit comes from a cancelled booking.')
    until = _credit_until(source)
    if until is None:
        raise LedgerError('This cancellation did not keep the deposit as credit.')
    if until < timezone.localdate():
        raise LedgerError(f'This credit expired on {until:%d %B %Y}.')
    if target.status not in (BookingStatus.PENDING, BookingStatus.CONFIRMED) \
            or target.end_at <= timezone.now():
        raise LedgerError('Credit goes to an upcoming pending or confirmed booking.')
    if timezone.localtime(target.end_at).date() > until:
        raise LedgerError(f'The new session must be completed by {until:%d %B %Y} '
                          '(30 days from the cancellation).')

    owed = outstanding(target)
    if owed <= 0:
        raise LedgerError('That booking is already paid in full.')

    # Take from the cancelled booking's payments, oldest first.
    remaining = owed
    moved = 0
    for txn in (Transaction.objects.select_for_update()
                .filter(booking=source, status=TransactionStatus.SUCCEEDED).order_by('created_at')):
        if remaining == 0:
            break
        take = min(balance(txn), remaining)
        if take <= 0:
            continue
        LedgerEntry.objects.create(
            transaction=txn, entry_type=EntryType.TRANSFER, amount_pesewas=take,
            beneficiary=Beneficiary.CUSTOMER, created_by=actor,
            note=f'Deposit credit to {target.reference}')
        moved += take
        remaining -= take
    if moved == 0:
        raise LedgerError('There is no credit left on that booking.')

    credit = open_transaction(target, Provider.CREDIT, amount_pesewas=moved,
                              provider_ref=f'{source.reference}>{target.reference}-'
                                           f'{secrets.token_hex(3).upper()}')
    mark_succeeded(credit, actor=actor, note=f'Deposit credit from {source.reference}')

    from apps.payments.checkout import deposit_for
    from apps.studio.money import net_paid
    if target.status == BookingStatus.PENDING and net_paid(target) >= deposit_for(target):
        target.status = BookingStatus.CONFIRMED
        target.save(update_fields=['status', 'updated_at'])
        AuditLog.record('booking.confirmed', actor=actor, target=target, request=request,
                        reference=target.reference, previous_status=BookingStatus.PENDING,
                        by_credit=True)
    for b, other in ((target, source), (source, target)):
        AuditLog.record('payment.credit_applied', actor=actor, target=b, request=request,
                        reference=b.reference, other_reference=other.reference,
                        amount_pesewas=moved, credit_until=until.isoformat())
    return credit
