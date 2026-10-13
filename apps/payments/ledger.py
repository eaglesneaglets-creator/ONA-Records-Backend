"""
The only way money enters or leaves the ledger.

Every function here takes a row lock on the Transaction (select_for_update)
before reading balances, so two concurrent requests — a provider callback
and its replay, or two staff clicking "release" — are serialised, and the
second sees the first's entry. Balances are always computed from entries,
never stored, so there is nothing to drift.

    open_transaction(booking, provider)       -> Transaction (pending)
    mark_succeeded(txn, provider_ref)         -> HOLD entry (idempotent)
    mark_failed(txn, reason)
    release(txn, beneficiary, actor)          -> RELEASE + Commission   [v0.4]
    refund(txn, amount, actor, note)          -> REFUND                 [Phase 2 policy]
    balance(txn)                              -> pesewas still held
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.db import transaction as db
from django.db.models import Sum

from apps.payments.money import split_commission

from .models import (
    Beneficiary, Commission, EntryType, LedgerEntry, Provider, Transaction, TransactionStatus,
)


class LedgerError(Exception):
    """A money operation that would break an invariant. Never swallowed."""


def studio_commission_percent() -> int:
    """
    The rate applied to NEW studio-booking transactions.

    Unset by ONA (spec OQ3): whether commission applies to ONA's own studio
    bookings at all is still open, so the default is 0. Changing the setting
    affects new transactions only — the rate is frozen on each one.
    """
    pct = getattr(settings, 'STUDIO_COMMISSION_PERCENT', 0)
    if type(pct) is not int or not 0 <= pct <= 100:
        raise LedgerError(f'STUDIO_COMMISSION_PERCENT must be an int 0–100, got {pct!r}.')
    return pct


def _totals(txn: Transaction) -> dict[str, int]:
    rows = (LedgerEntry.objects.filter(transaction=txn)
            .values('entry_type').annotate(total=Sum('amount_pesewas')))
    totals = {t: 0 for t in EntryType.values}
    for r in rows:
        totals[r['entry_type']] = r['total']
    return totals


def balance(txn: Transaction) -> int:
    """Pesewas ONA still holds for this transaction."""
    t = _totals(txn)
    return t[EntryType.HOLD] - t[EntryType.RELEASE] - t[EntryType.REFUND]


def _locked(txn: Transaction) -> Transaction:
    return Transaction.objects.select_for_update().get(pk=txn.pk)


def open_transaction(booking, provider: str = Provider.HUBTEL) -> Transaction:
    """A pending payment for a booking, at the booking's frozen total."""
    if booking.total_pesewas <= 0:
        raise LedgerError('A booking with no total cannot be paid for.')
    return Transaction.objects.create(
        customer=booking.customer, booking=booking, provider=provider,
        amount_pesewas=booking.total_pesewas,
        commission_percent=studio_commission_percent(),
    )


@db.atomic
def mark_succeeded(txn: Transaction, provider_ref: str | None = None,
                   actor=None) -> LedgerEntry:
    """
    The provider confirmed the money. Hold it — exactly once.

    Idempotent: a second call (a replayed callback) returns the existing hold
    and changes nothing. A transaction already marked failed cannot succeed
    here; that needs a human to look at it.
    """
    txn = _locked(txn)
    existing = LedgerEntry.objects.filter(transaction=txn, entry_type=EntryType.HOLD).first()
    if existing is not None:
        return existing
    if txn.status == TransactionStatus.FAILED:
        raise LedgerError('This transaction already failed; it cannot now succeed.')

    txn.status = TransactionStatus.SUCCEEDED
    if provider_ref:
        txn.provider_ref = provider_ref
    txn.save(update_fields=['status', 'provider_ref', 'updated_at'])
    hold = LedgerEntry.objects.create(
        transaction=txn, entry_type=EntryType.HOLD, amount_pesewas=txn.amount_pesewas,
        beneficiary=Beneficiary.ONA, created_by=actor,
    )
    # Same database transaction as the hold: a receipt exists exactly when
    # the money does.
    from apps.payments.documents import issue_receipt
    issue_receipt(txn)
    return hold


@db.atomic
def mark_failed(txn: Transaction, reason: str = '') -> Transaction:
    txn = _locked(txn)
    if txn.status == TransactionStatus.SUCCEEDED:
        raise LedgerError('This transaction succeeded; refund it rather than failing it.')
    txn.status = TransactionStatus.FAILED
    txn.failure_reason = reason[:255]
    txn.save(update_fields=['status', 'failure_reason', 'updated_at'])
    return txn


@dataclass(frozen=True)
class Released:
    entry: LedgerEntry
    commission: Commission
    payout_pesewas: int


@db.atomic
def release(txn: Transaction, beneficiary: str = Beneficiary.ONA, actor=None) -> Released:
    """
    Release everything still held, minus commission at the transaction's
    frozen rate. Not wired to anything in v0.1 (escrow release is v0.4).
    """
    txn = _locked(txn)
    held = balance(txn)
    if held <= 0:
        raise LedgerError('Nothing is held for this transaction.')
    if beneficiary == Beneficiary.CUSTOMER:
        raise LedgerError('Money back to the customer is a refund, not a release.')

    split = split_commission(held, txn.commission_percent)
    entry = LedgerEntry.objects.create(
        transaction=txn, entry_type=EntryType.RELEASE, amount_pesewas=held,
        beneficiary=beneficiary, created_by=actor,
    )
    commission = Commission.objects.create(
        ledger_entry=entry, percent=split.commission_percent,
        amount_pesewas=split.commission_pesewas,
    )
    return Released(entry=entry, commission=commission,
                    payout_pesewas=split.professional_pesewas)


@db.atomic
def refund(txn: Transaction, amount_pesewas: int, actor=None, note: str = '') -> LedgerEntry:
    """Return held money to the customer. Never more than is still held."""
    if type(amount_pesewas) is not int or amount_pesewas <= 0:
        raise LedgerError('Refund amount must be a positive whole number of pesewas.')
    txn = _locked(txn)
    held = balance(txn)
    if amount_pesewas > held:
        raise LedgerError(f'Cannot refund {amount_pesewas}; only {held} is held.')
    return LedgerEntry.objects.create(
        transaction=txn, entry_type=EntryType.REFUND, amount_pesewas=amount_pesewas,
        beneficiary=Beneficiary.CUSTOMER, created_by=actor, note=note[:255],
    )
