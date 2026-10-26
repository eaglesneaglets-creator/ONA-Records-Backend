"""
Issuing invoices and receipts.

    issue_invoice(booking)      -> Invoice   (idempotent: one per booking)
    revise_invoice(booking)     -> Invoice   (before any payment only)
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
    room_total = booking.room_total_pesewas
    unit = room_total // hours if hours else room_total
    # The rate is derived from the frozen total, not read from the room: the
    # room's rate may have changed since the booking was priced.
    if unit * hours != room_total:
        # Never fudge a line to make it add up — show one line at the total.
        lines = [{
            'description': f'{booking.room.name} with engineer — {booking.service.name}',
            'quantity': 1, 'unit_pesewas': room_total, 'total_pesewas': room_total,
        }]
    else:
        lines = [{
            # The unit price has its own column; repeating it here crowded the line.
            'description': f'{booking.room.name} · {booking.service.name} · engineer included, '
                           f'per hour',
            'quantity': hours, 'unit_pesewas': unit, 'total_pesewas': room_total,
        }]
    pro = booking.assigned_professional
    if pro is not None and booking.professional_fee_pesewas:
        # The customer-facing name only (stage name if set) — never contact
        # details, which stay with ONA (PRODUCT.md constraint 1).
        lines.append({
            'description': f'Session professional · {pro.stage_name or pro.name}',
            'quantity': 1, 'unit_pesewas': booking.professional_fee_pesewas,
            'total_pesewas': booking.professional_fee_pesewas,
        })
    return lines


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


def has_payment(booking) -> bool:
    return Transaction.objects.filter(booking=booking,
                                      status=TransactionStatus.SUCCEEDED).exists()


@db.atomic
def revise_invoice(booking) -> Invoice:
    """
    Re-price the booking's invoice after staff change what it includes.

    Keeps the number (no gap, no second invoice for one booking) and bumps
    the revision. Refused once a payment has succeeded: from then on the
    invoice is what was paid against, and it never changes.
    """
    invoice = Invoice.objects.select_for_update().filter(booking=booking).first()
    if invoice is None:
        return issue_invoice(booking)
    if has_payment(booking):
        raise DocumentError('A payment has been made against this invoice; it can no longer change.')
    lines = _invoice_lines(booking)
    total = sum(line['total_pesewas'] for line in lines)
    if total != booking.total_pesewas:
        raise DocumentError('Invoice lines do not add up to the booking total.')
    if lines == invoice.lines and total == invoice.total_pesewas:
        return invoice
    invoice.lines, invoice.total_pesewas = lines, total
    invoice.revision += 1
    invoice.revised_at = timezone.now()
    invoice.save(update_fields=['lines', 'total_pesewas', 'revision', 'revised_at'])
    return invoice


@db.atomic
def issue_receipt(txn: Transaction) -> Receipt:
    existing = Receipt.objects.filter(transaction=txn).first()
    if existing is not None:
        return existing
    if txn.status != TransactionStatus.SUCCEEDED:
        raise DocumentError('A receipt is only issued for a confirmed payment.')
    return Receipt.objects.create(transaction=txn, number=next_number('RCT'),
                                  amount_pesewas=txn.amount_pesewas)
