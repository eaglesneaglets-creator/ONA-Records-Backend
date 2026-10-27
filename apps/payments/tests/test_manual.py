"""
Task 16 — money recorded by staff: off-platform payments and refunds.

The acceptance line: "a manual ledger entry is flagged as such and carries
an actor". Also: it goes through the same ledger as a provider payment
(hold, receipt, email), never exceeds what is owed or held, and a repeated
reference is the same money, not a second payment.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core import mail
from django.urls import reverse
from rest_framework.test import APIClient

from apps.payments import ledger
from apps.payments.models import EntryType, LedgerEntry, Provider, Receipt, Transaction
from apps.studio.models import BookingStatus, Engineer, Room, Service, StudioBooking
from core.models import AuditLog

pytestmark = pytest.mark.django_db
TZ = ZoneInfo('Africa/Accra')


def at(days, hour):
    d = date.today() + timedelta(days=days)
    return datetime.combine(d, datetime.min.time(), tzinfo=TZ) + timedelta(hours=hour)


@pytest.fixture
def w(django_user_model):
    U = django_user_model.objects
    admin = U.create_user(email='staff@x.test', password='p-Ass-1', role='admin')
    ama = U.create_user(email='ama@x.test', password='p-Ass-1', role='customer', first_name='Ama')
    room = Room.objects.create(name='A', hourly_rate_pesewas=20000)
    booking = StudioBooking.objects.create(
        customer=ama, room=room, engineer=Engineer.objects.create(name='One'),
        service=Service.objects.create(name='Rec', base_price_pesewas=1, duration_minutes=60),
        start_at=at(5, 10), end_at=at(5, 12), total_pesewas=40000)
    c = APIClient()
    c.force_authenticate(admin)
    return {'admin': admin, 'ama': ama, 'b': booking, 'staff': c}


def pay(w, **over):
    data = {'amount_pesewas': 15000, 'method': 'mobile_money', 'reference': 'MOMO-123',
            'note': 'Deposit by MoMo'}
    data.update(over)
    return w['staff'].post(reverse('adminapi:booking-record-payment', args=[w['b'].pk]), data,
                           format='json')


class TestRecordPayment:
    def test_a_manual_entry_is_flagged_and_carries_an_actor(
            self, w, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            res = pay(w)
        assert res.status_code == 201, res.data
        txn = Transaction.objects.get(booking=w['b'])
        assert txn.provider == Provider.MANUAL and txn.method == 'mobile_money'
        assert txn.provider_ref == 'MOMO-123' and txn.amount_pesewas == 15000
        hold = LedgerEntry.objects.get(transaction=txn)
        assert hold.entry_type == EntryType.HOLD
        assert hold.created_by == w['admin'] and hold.note == 'Deposit by MoMo'
        assert Receipt.objects.filter(transaction=txn).exists()
        assert [m.subject for m in mail.outbox] == [f'Payment received — {w["b"].reference}']

        log = AuditLog.objects.get(action='payment.held')
        assert log.actor == w['admin'] and log.metadata['manual'] is True
        assert log.metadata['amount_pesewas'] == 15000 and log.metadata['method'] == 'mobile_money'

        # The console shows it as manual, entry by entry.
        assert res.data['paid_pesewas'] == 15000
        assert res.data['outstanding_pesewas'] == 25000
        assert res.data['outstanding_display'] == 'GHS 250.00'
        [t] = res.data['transactions']
        assert t['is_manual'] and t['method_display'] == 'Mobile Money'
        assert t['provider_display'] == 'Recorded by ONA staff'
        assert t['ledger'][0]['recorded_by_staff'] is True
        assert t['ledger'][0]['created_by_email'] == 'staff@x.test'

    def test_a_provider_payment_is_not_marked_as_staff(self, w):
        txn = ledger.open_transaction(w['b'])
        ledger.mark_succeeded(txn, provider_ref='HUB-1')
        detail = w['staff'].get(reverse('adminapi:booking-detail', args=[w['b'].pk])).data
        [t] = detail['transactions']
        assert not t['is_manual'] and t['ledger'][0]['recorded_by_staff'] is False

    def test_cannot_take_more_than_is_owed(self, w):
        assert pay(w, amount_pesewas=30000).status_code == 201
        res = pay(w, amount_pesewas=10001, reference='MOMO-2')
        assert res.status_code == 400 and 'more than is owed' in res.data['detail']
        assert pay(w, amount_pesewas=10000, reference='MOMO-3').status_code == 201
        res = pay(w, amount_pesewas=1, reference='MOMO-4')
        assert 'paid in full' in res.data['detail']

    def test_the_same_reference_is_the_same_money(self, w):
        assert pay(w).status_code == 201
        res = pay(w)
        assert res.status_code == 400 and 'already been recorded' in res.data['detail']
        assert Transaction.objects.count() == 1

    def test_cash_without_a_reference_can_be_recorded_twice(self, w):
        assert pay(w, method='cash', reference='', amount_pesewas=10000).status_code == 201
        assert pay(w, method='cash', reference='', amount_pesewas=10000).status_code == 201

    def test_not_on_a_cancelled_booking(self, w):
        w['b'].status = BookingStatus.CANCELLED
        w['b'].save()
        res = pay(w)
        assert res.status_code == 400 and 'cancelled' in res.data['detail']

    @pytest.mark.parametrize('bad', [{'method': 'bitcoin'}, {'amount_pesewas': 0},
                                     {'amount_pesewas': 1.5}])
    def test_bad_input(self, w, bad):
        assert pay(w, **bad).status_code == 400

    def test_customers_cannot_record_payments(self, w):
        c = APIClient()
        c.force_authenticate(w['ama'])
        res = c.post(reverse('adminapi:booking-record-payment', args=[w['b'].pk]),
                     {'amount_pesewas': 100, 'method': 'cash'}, format='json')
        assert res.status_code == 403 and not Transaction.objects.exists()


class TestRecordRefund:
    def refund(self, w, txn, **over):
        data = {'amount_pesewas': 5000, 'note': 'Returned by MoMo, ref R-77'}
        data.update(over)
        return w['staff'].post(reverse('adminapi:payment-refund', args=[txn.pk]), data,
                               format='json')

    def test_refund_is_recorded_with_actor_and_emailed(
            self, w, django_capture_on_commit_callbacks):
        pay(w)
        txn = Transaction.objects.get()
        with django_capture_on_commit_callbacks(execute=True):
            res = self.refund(w, txn)
        assert res.status_code == 200, res.data
        entry = LedgerEntry.objects.get(transaction=txn, entry_type=EntryType.REFUND)
        assert entry.created_by == w['admin'] and entry.note == 'Returned by MoMo, ref R-77'
        assert res.data['held_pesewas'] == 10000
        assert any(m.subject == f'Refund recorded — {w["b"].reference}' for m in mail.outbox)
        log = AuditLog.objects.get(action='payment.refunded')
        assert log.actor == w['admin'] and log.metadata['amount_pesewas'] == 5000

        # Refunded money is owed again.
        assert pay(w, amount_pesewas=30000, reference='MOMO-9').status_code == 201

    def test_an_online_payment_can_be_refunded_by_staff_too(self, w):
        txn = ledger.open_transaction(w['b'])
        ledger.mark_succeeded(txn, provider_ref='HUB-2')
        assert self.refund(w, txn, amount_pesewas=40000).status_code == 200

    def test_never_more_than_is_held(self, w):
        pay(w)
        res = self.refund(w, Transaction.objects.get(), amount_pesewas=15001)
        assert res.status_code == 400 and 'only 15000 is held' in res.data['detail']

    def test_needs_a_note(self, w):
        pay(w)
        res = self.refund(w, Transaction.objects.get(), note='   ')
        assert res.status_code == 400

    def test_only_a_confirmed_payment(self, w):
        txn = ledger.open_transaction(w['b'])  # pending, never succeeded
        res = self.refund(w, txn)
        assert res.status_code == 400 and 'confirmed payment' in res.data['detail']
