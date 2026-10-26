"""
What has been paid on a booking, net of refunds — one definition for the
customer API, the staff console and the emails (QA F1).

Correlated subqueries, not joins: a booking with several payments and
several ledger rows would otherwise be double-counted.
"""

from django.db.models import IntegerField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce


def _sum(qs, field='amount_pesewas'):
    # order_by() drops the model's default ordering, which Postgres would
    # otherwise demand in the GROUP BY.
    return Coalesce(
        Subquery(qs.order_by().values(_k=Value(1)).annotate(t=Sum(field)).values('t')[:1],
                 output_field=IntegerField()),
        Value(0),
    )


def with_paid(qs):
    """Annotate paid_pesewas (succeeded payments) and refunded_pesewas."""
    from apps.payments.models import EntryType, LedgerEntry, Transaction, TransactionStatus

    paid = Transaction.objects.filter(booking=OuterRef('pk'), status=TransactionStatus.SUCCEEDED)
    refunded = LedgerEntry.objects.filter(transaction__booking=OuterRef('pk'),
                                          entry_type=EntryType.REFUND)
    return qs.annotate(paid_pesewas=_sum(paid), refunded_pesewas=_sum(refunded))


def net_paid(booking) -> int:
    """Net pesewas held for one booking, annotated or not."""
    if hasattr(booking, 'paid_pesewas') and hasattr(booking, 'refunded_pesewas'):
        return booking.paid_pesewas - booking.refunded_pesewas
    from apps.payments.models import EntryType, LedgerEntry, Transaction, TransactionStatus
    paid = (Transaction.objects.filter(booking=booking, status=TransactionStatus.SUCCEEDED)
            .aggregate(t=Sum('amount_pesewas'))['t'] or 0)
    refunded = (LedgerEntry.objects.filter(transaction__booking=booking, entry_type=EntryType.REFUND)
                .aggregate(t=Sum('amount_pesewas'))['t'] or 0)
    return paid - refunded
