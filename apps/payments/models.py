"""
The money schema — escrow-ready from day one (ADR-007).

Three tables, and the rules that make them trustworthy:

  Transaction   one attempt to take a payment: who, for what, how much,
                through which provider, and the commission rate in force.
  LedgerEntry   what happened to money ONA holds: hold, release, refund.
                APPEND-ONLY. An entry is never edited or deleted — a
                correction is a new entry. In a dispute the ledger is the
                evidence, and evidence that can be rewritten is not evidence.
  Commission    ONA's share, attached to the release it was taken from.

INVARIANTS (enforced in apps/payments/ledger.py and proven by tests):

  * Every amount is an integer number of pesewas, > 0.
  * For a transaction: released + refunded <= held, always.
  * For a release: payout + commission == released amount, exactly
    (apps/payments/money.py — commission rounds down, remainder to the payee).
  * The commission RATE is frozen on the Transaction at creation. ONA has not
    yet set the rate, nor decided whether studio bookings carry commission
    (spec OQ3); when they do, it is a setting change that affects new
    transactions only — no stored money is ever re-priced.

v0.1 exercises only HOLD (a confirmed payment). Release and refund are built
and tested here so the schema is proven, and are wired up in v0.4 (escrow
release) and Phase 2's cancellation/refund policy respectively.
"""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.payments.money import format_ghs
from core.mixins.timestamp import TimestampMixin, UUIDPrimaryKeyMixin


class Provider(models.TextChoices):
    HUBTEL = 'hubtel', 'Hubtel'
    # Money ONA received outside the platform and recorded by hand (Task 16).
    MANUAL = 'manual', 'Recorded by ONA staff'


class TransactionStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'          # charge started, provider has not answered
    SUCCEEDED = 'succeeded', 'Succeeded'    # provider confirmed; money is with ONA
    FAILED = 'failed', 'Failed'             # provider refused, or it timed out


class Transaction(UUIDPrimaryKeyMixin, TimestampMixin):
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                 related_name='transactions')
    # v0.1 pays for studio bookings only. Projects (v0.3) add a sibling FK and
    # widen the check constraint below — additive, not a rebuild.
    booking = models.ForeignKey('studio.StudioBooking', on_delete=models.PROTECT,
                                related_name='transactions')

    provider = models.CharField(max_length=16, choices=Provider.choices)
    provider_ref = models.CharField(
        max_length=100, null=True, blank=True,
        help_text="The provider's id for this charge. Unique per provider — it is the "
                  'idempotency key a replayed callback is matched on.',
    )
    amount_pesewas = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    currency = models.CharField(max_length=3, default='GHS', editable=False)
    status = models.CharField(max_length=10, choices=TransactionStatus.choices,
                              default=TransactionStatus.PENDING, db_index=True)
    failure_reason = models.CharField(max_length=255, blank=True)

    commission_percent = models.PositiveSmallIntegerField(
        validators=[MaxValueValidator(100)],
        help_text='Frozen at creation. A later change to the configured rate never '
                  're-prices this transaction.',
    )

    class Meta:
        ordering = ('-created_at',)
        constraints = [
            models.CheckConstraint(condition=models.Q(amount_pesewas__gt=0),
                                   name='payments_txn_amount_positive'),
            models.CheckConstraint(condition=models.Q(commission_percent__lte=100),
                                   name='payments_txn_commission_percent_max'),
            models.CheckConstraint(condition=models.Q(currency='GHS'),
                                   name='payments_txn_currency_ghs'),
            models.UniqueConstraint(
                fields=['provider', 'provider_ref'],
                condition=models.Q(provider_ref__isnull=False),
                name='payments_txn_provider_ref_unique',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.get_provider_display()} {self.amount_display} · {self.status}'

    @property
    def amount_display(self) -> str:
        return format_ghs(self.amount_pesewas)


class EntryType(models.TextChoices):
    HOLD = 'hold', 'Held by ONA'
    RELEASE = 'release', 'Released to the payee'
    REFUND = 'refund', 'Refunded to the customer'


class Beneficiary(models.TextChoices):
    ONA = 'ona', 'ONA Records'                # holds; studio sessions are ONA's own work
    PROFESSIONAL = 'professional', 'Professional'  # marketplace payouts (v0.3+)
    CUSTOMER = 'customer', 'Customer'         # refunds


class LedgerEntryQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise TypeError('Ledger entries are append-only: add a correcting entry instead.')

    def delete(self):
        raise TypeError('Ledger entries are append-only and cannot be deleted.')


class LedgerEntry(UUIDPrimaryKeyMixin):
    """
    One movement of held money. Create through apps/payments/ledger.py, which
    enforces the balance rules under a row lock; never directly.

    Append-only at three levels: save() refuses an update, the queryset
    refuses bulk update/delete, and delete() refuses. (A database trigger
    would be the fourth; worth adding before escrow release goes live.)
    """

    transaction = models.ForeignKey(Transaction, on_delete=models.PROTECT,
                                    related_name='ledger_entries')
    entry_type = models.CharField(max_length=8, choices=EntryType.choices)
    amount_pesewas = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    beneficiary = models.CharField(max_length=16, choices=Beneficiary.choices)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+', help_text='Staff member who triggered it. Null for the system.',
    )
    note = models.CharField(max_length=255, blank=True)

    objects = LedgerEntryQuerySet.as_manager()

    class Meta:
        ordering = ('created_at',)
        verbose_name_plural = 'ledger entries'
        constraints = [
            models.CheckConstraint(condition=models.Q(amount_pesewas__gt=0),
                                   name='payments_ledger_amount_positive'),
            # Exactly one hold per transaction: a replayed provider callback
            # cannot hold the same money twice, even if the code is wrong.
            models.UniqueConstraint(
                fields=['transaction'], condition=models.Q(entry_type='hold'),
                name='payments_ledger_one_hold_per_txn',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.get_entry_type_display()} {format_ghs(self.amount_pesewas)}'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError('Ledger entries are append-only: add a correcting entry instead.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError('Ledger entries are append-only and cannot be deleted.')


class Commission(UUIDPrimaryKeyMixin):
    """ONA's share of a release. payout = release amount - this amount."""

    ledger_entry = models.OneToOneField(LedgerEntry, on_delete=models.PROTECT,
                                        related_name='commission')
    percent = models.PositiveSmallIntegerField(validators=[MaxValueValidator(100)])
    amount_pesewas = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(percent__lte=100),
                                   name='payments_commission_percent_max'),
        ]

    def __str__(self) -> str:
        return f'{self.percent}% · {format_ghs(self.amount_pesewas)}'


# ---------------------------------------------------------------------------
# Documents — invoices and receipts (Task 10)
# ---------------------------------------------------------------------------

class DocumentCounter(models.Model):
    """
    Per-kind, per-year sequence for document numbers: INV-2026-00001.

    A row lock (select_for_update) makes numbers gapless and unique under
    concurrency, which a database sequence does not guarantee (it skips on
    rollback). Accountants ask about gaps; there should not be any.
    """

    kind = models.CharField(max_length=3)
    year = models.PositiveSmallIntegerField()
    last = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['kind', 'year'], name='payments_doccounter_kind_year'),
        ]


class Invoice(UUIDPrimaryKeyMixin):
    """
    What the customer owes for a booking — FROZEN at issue.

    Number, parties, line items and total are copied in when the invoice is
    issued, so renaming a room or changing a rate later never alters an
    issued invoice. The session's date and time are deliberately NOT frozen:
    a booking can be rescheduled without changing what is owed, and the PDF
    shows the session as it currently stands, labelled as such.
    """

    booking = models.OneToOneField('studio.StudioBooking', on_delete=models.PROTECT,
                                   related_name='invoice')
    number = models.CharField(max_length=20, unique=True)
    issued_at = models.DateTimeField(auto_now_add=True)
    customer_name = models.CharField(max_length=200)
    customer_email = models.EmailField()
    lines = models.JSONField(
        help_text='[{description, quantity, unit_pesewas, total_pesewas}] — integers only.')
    total_pesewas = models.PositiveIntegerField()

    class Meta:
        ordering = ('-issued_at',)
        constraints = [
            models.CheckConstraint(condition=models.Q(total_pesewas__gt=0),
                                   name='payments_invoice_total_positive'),
        ]

    def __str__(self) -> str:
        return f'{self.number} · {format_ghs(self.total_pesewas)}'


class Receipt(UUIDPrimaryKeyMixin):
    """Proof of a confirmed payment. Issued with the HOLD, once per transaction."""

    transaction = models.OneToOneField(Transaction, on_delete=models.PROTECT,
                                       related_name='receipt')
    number = models.CharField(max_length=20, unique=True)
    issued_at = models.DateTimeField(auto_now_add=True)
    amount_pesewas = models.PositiveIntegerField()

    class Meta:
        ordering = ('-issued_at',)

    def __str__(self) -> str:
        return f'{self.number} · {format_ghs(self.amount_pesewas)}'
