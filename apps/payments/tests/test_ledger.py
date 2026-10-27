"""
Task 7 — the ledger.

What has to be true, whatever the code calling it does:
  * money in == money out + money still held, to the pesewa;
  * a hold happens once per transaction, even if the callback is replayed;
  * nothing can be released or refunded that is not held;
  * ledger entries can never be edited or deleted;
  * the commission rate is frozen when the transaction is created.
"""

import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.db import IntegrityError, transaction

from apps.payments.ledger import (
    LedgerError, balance, mark_failed, mark_succeeded, open_transaction, refund, release,
)
from apps.payments.models import (
    Beneficiary, Commission, EntryType, LedgerEntry, Provider, Transaction, TransactionStatus,
)
from apps.studio.models import Engineer, Room, Service, StudioBooking

pytestmark = pytest.mark.django_db

TZ = ZoneInfo('Africa/Accra')


@pytest.fixture
def world(django_user_model):
    return {
        'customer': django_user_model.objects.create_user(email='c@x.test', password='p-Ass-1'),
        'room': Room.objects.create(name='A', hourly_rate_pesewas=20000),
        'eng': Engineer.objects.create(name='One'),
        'svc': Service.objects.create(name='Rec', base_price_pesewas=1, duration_minutes=60),
    }


_slot = iter(range(10_000))


def make_booking(w, total=80000):
    # Each booking on its own day, so the double-booking rule never interferes.
    day = date.today() + timedelta(days=10 + next(_slot))
    start = datetime.combine(day, datetime.min.time(), tzinfo=TZ) + timedelta(hours=10)
    return StudioBooking.objects.create(
        customer=w['customer'], room=w['room'], engineer=w['eng'], service=w['svc'],
        start_at=start, end_at=start + timedelta(hours=1), total_pesewas=total)


@pytest.fixture
def txn(world):
    return open_transaction(make_booking(world))


class TestOpen:
    def test_takes_the_bookings_frozen_total(self, txn):
        assert txn.amount_pesewas == 80000
        assert txn.status == TransactionStatus.PENDING
        assert txn.currency == 'GHS'
        assert balance(txn) == 0

    def test_freezes_the_commission_rate_at_creation(self, world, settings):
        settings.STUDIO_COMMISSION_PERCENT = 15
        t = open_transaction(make_booking(world))
        settings.STUDIO_COMMISSION_PERCENT = 40
        t.refresh_from_db()
        assert t.commission_percent == 15
        assert release_after_hold(t).commission.percent == 15

    def test_studio_commission_defaults_to_zero_until_ona_decides(self, txn):
        assert txn.commission_percent == 0

    def test_rejects_an_impossible_configured_rate(self, world, settings):
        settings.STUDIO_COMMISSION_PERCENT = 150
        with pytest.raises(LedgerError):
            open_transaction(make_booking(world))


def release_after_hold(t, **kw):
    mark_succeeded(t, provider_ref=f'ref-{t.pk}')
    return release(t, **kw)


class TestHold:
    def test_success_holds_the_full_amount(self, txn):
        entry = mark_succeeded(txn, provider_ref='HUB-1')
        txn.refresh_from_db()
        assert txn.status == TransactionStatus.SUCCEEDED and txn.provider_ref == 'HUB-1'
        assert entry.entry_type == EntryType.HOLD and entry.amount_pesewas == 80000
        assert entry.beneficiary == Beneficiary.ONA
        assert balance(txn) == 80000

    def test_a_replayed_callback_does_not_hold_twice(self, txn):
        first = mark_succeeded(txn, provider_ref='HUB-1')
        second = mark_succeeded(txn, provider_ref='HUB-1')
        assert first.pk == second.pk
        assert LedgerEntry.objects.filter(transaction=txn).count() == 1
        assert balance(txn) == 80000

    def test_the_database_refuses_a_second_hold_even_if_the_code_is_bypassed(self, txn):
        mark_succeeded(txn)
        with pytest.raises(IntegrityError), transaction.atomic():
            LedgerEntry.objects.create(transaction=txn, entry_type=EntryType.HOLD,
                                       amount_pesewas=1, beneficiary=Beneficiary.ONA)

    def test_a_failed_transaction_cannot_then_succeed(self, txn):
        mark_failed(txn, 'customer declined the prompt')
        with pytest.raises(LedgerError):
            mark_succeeded(txn)
        assert balance(txn) == 0

    def test_a_succeeded_transaction_cannot_be_marked_failed(self, txn):
        mark_succeeded(txn)
        with pytest.raises(LedgerError):
            mark_failed(txn)

    def test_provider_ref_is_unique_per_provider(self, world):
        a = open_transaction(make_booking(world))
        b = open_transaction(make_booking(world))
        mark_succeeded(a, provider_ref='SAME')
        with pytest.raises(IntegrityError), transaction.atomic():
            mark_succeeded(b, provider_ref='SAME')

    def test_the_same_ref_from_different_providers_is_fine(self, world):
        a = open_transaction(make_booking(world), provider=Provider.PAYSTACK)
        b = open_transaction(make_booking(world), provider=Provider.MANUAL)
        mark_succeeded(a, provider_ref='X1')
        mark_succeeded(b, provider_ref='X1')


class TestReleaseAndRefund:
    def test_release_splits_by_the_frozen_rate_and_reconciles(self, world, settings):
        settings.STUDIO_COMMISSION_PERCENT = 15
        t = open_transaction(make_booking(world, total=125000))
        r = release_after_hold(t, beneficiary=Beneficiary.PROFESSIONAL)
        assert r.entry.amount_pesewas == 125000
        assert r.commission.amount_pesewas == 18750
        assert r.payout_pesewas == 106250
        assert r.payout_pesewas + r.commission.amount_pesewas == r.entry.amount_pesewas
        assert balance(t) == 0

    def test_commission_rounds_down_in_the_payees_favour(self, world, settings):
        settings.STUDIO_COMMISSION_PERCENT = 15
        t = open_transaction(make_booking(world, total=101))
        r = release_after_hold(t)
        assert (r.commission.amount_pesewas, r.payout_pesewas) == (15, 86)

    def test_cannot_release_what_is_not_held(self, txn):
        with pytest.raises(LedgerError):
            release(txn)                     # never held
        mark_succeeded(txn)
        release(txn)
        with pytest.raises(LedgerError):
            release(txn)                     # already released

    def test_release_to_the_customer_is_refused(self, txn):
        mark_succeeded(txn)
        with pytest.raises(LedgerError):
            release(txn, beneficiary=Beneficiary.CUSTOMER)

    def test_partial_refund_then_release_of_the_rest(self, txn):
        mark_succeeded(txn)
        refund(txn, 30000, note='one hour cancelled')
        assert balance(txn) == 50000
        r = release(txn)
        assert r.entry.amount_pesewas == 50000
        assert balance(txn) == 0

    def test_cannot_refund_more_than_is_held(self, txn):
        mark_succeeded(txn)
        with pytest.raises(LedgerError):
            refund(txn, 80001)

    @pytest.mark.parametrize('bad', [0, -1, 1.5, '100'])
    def test_refund_amount_must_be_positive_whole_pesewas(self, txn, bad):
        mark_succeeded(txn)
        with pytest.raises(LedgerError):
            refund(txn, bad)


class TestAppendOnly:
    def test_an_entry_cannot_be_edited(self, txn):
        e = mark_succeeded(txn)
        e.amount_pesewas = 1
        with pytest.raises(TypeError):
            e.save()

    def test_an_entry_cannot_be_deleted(self, txn):
        e = mark_succeeded(txn)
        with pytest.raises(TypeError):
            e.delete()

    def test_bulk_update_and_delete_are_refused(self, txn):
        mark_succeeded(txn)
        with pytest.raises(TypeError):
            LedgerEntry.objects.filter(transaction=txn).update(amount_pesewas=1)
        with pytest.raises(TypeError):
            LedgerEntry.objects.filter(transaction=txn).delete()
        assert LedgerEntry.objects.get(transaction=txn).amount_pesewas == 80000

    def test_a_booking_with_money_cannot_be_hard_deleted(self, txn):
        mark_succeeded(txn)
        from django.db.models import ProtectedError
        with pytest.raises(ProtectedError):
            txn.booking.delete()


class TestReconciliation:
    @pytest.mark.parametrize('seed', range(5))
    def test_money_in_equals_money_out_plus_held(self, world, settings, seed):
        """Random amounts, rates, partial refunds and releases: every pesewa
        that came in is accounted for exactly once."""
        rng = random.Random(seed)
        total_in = total_out = 0
        open_txns = []
        for _ in range(40):
            settings.STUDIO_COMMISSION_PERCENT = rng.randint(0, 100)
            amount = rng.randint(1, 5_000_000)
            t = open_transaction(make_booking(world, total=amount))
            mark_succeeded(t)
            total_in += amount
            if rng.random() < 0.5:
                part = rng.randint(1, amount)
                refund(t, part)
                total_out += part
            if rng.random() < 0.6 and balance(t) > 0:
                r = release(t)
                assert r.payout_pesewas + r.commission.amount_pesewas == r.entry.amount_pesewas
                total_out += r.entry.amount_pesewas
            open_txns.append(t)
        still_held = sum(balance(t) for t in open_txns)
        assert total_in == total_out + still_held
        assert all(balance(t) >= 0 for t in open_txns)
        assert Commission.objects.count() == LedgerEntry.objects.filter(
            entry_type=EntryType.RELEASE).count()
