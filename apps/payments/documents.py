"""
Issuing invoices and receipts.

    issue_invoice(booking)      -> Invoice   (idempotent: one per booking)
    issue_receipt(transaction)  -> Receipt   (idempotent: one per transaction)

Both freeze their figures at issue. Numbers come from DocumentCounter under a
row lock: gapless, unique, per year — INV-2026-00001, RCT-2026-00001.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction as db
from django.utils import timezone

from .models import DocumentCounter, Invoice, Receipt, Transaction, TransactionStatus


class DocumentError(Exception):
    pass


def next_number(kind: str) -> str:
    """Next gapless number for this kind and year. Call inside a transaction."""
    year = timezone.localdate().year
    try:
        with db.atomic():
            counter, _ = DocumentCounter.objects.select_for_update().get_or_create(
                kind=kind, year=year)
    except IntegrityError:
        # Two first-of-the-year requests both tried to create the row; the
        # loser reads the winner's.
        counter = DocumentCounter.objects.select_for_update().get(kind=kind, year=year)
    counter.last += 1
    counter.save(update_fields=['last'])
    return f'{kind}-{year}-{counter.last:05d}'


def _invoice_lines(booking) -> list[dict]:
    hours = booking.duration_minutes // 60
    unit = booking.total_pesewas // hours if hours else booking.total_pesewas
    # The rate is derived from the frozen total, not read from the room: the
    # room's rate may have changed since the booking was priced.
    if unit * hours != booking.total_pesewas:
        # Never fudge a line to make it add up — show one line at the total.
        return [{
            'description': f'{booking.room.name} with engineer — {booking.service.name}',
            'quantity': 1, 'unit_pesewas': booking.total_pesewas,
            'total_pesewas': booking.total_pesewas,
        }]
    return [{
        # The unit price has its own column; repeating it here crowded the line.
        'description': f'{booking.room.name} · {booking.service.name} · engineer included, '
                       f'per hour',
        'quantity': hours, 'unit_pesewas': unit, 'total_pesewas': booking.total_pesewas,
    }]


@db.atomic
def issue_invoice(booking) -> Invoice:
    existing = Invoice.objects.filter(booking=booking).first()
    if existing is not None:
        return existing
    lines = _invoice_lines(booking)
    total = sum(line['total_pesewas'] for line in lines)
    if total != booking.total_pesewas:
        raise DocumentError('Invoice lines do not add up to the booking total.')
    customer = booking.customer
    name = f'{customer.first_name} {customer.last_name}'.strip() or customer.email
    return Invoice.objects.create(
        booking=booking, number=next_number('INV'), customer_name=name,
        customer_email=customer.email, lines=lines, total_pesewas=total,
    )


@db.atomic
def issue_receipt(txn: Transaction) -> Receipt:
    existing = Receipt.objects.filter(transaction=txn).first()
    if existing is not None:
        return existing
    if txn.status != TransactionStatus.SUCCEEDED:
        raise DocumentError('A receipt is only issued for a confirmed payment.')
    return Receipt.objects.create(transaction=txn, number=next_number('RCT'),
                                  amount_pesewas=txn.amount_pesewas)
