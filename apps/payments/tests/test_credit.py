"""
Deposit credit (ONA's terms): a deposit kept on a 48-hour-notice cancellation
moves to a new session completed within 30 days. Staff apply it; the money
never leaves ONA and the ledger shows both ends.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.payments.credit import apply_credit, available_credits
from apps.payments.ledger import LedgerError
from apps.payments.manual import outstanding, record_payment
from apps.payments.models import EntryType, LedgerEntry, Provider, Transaction
from apps.studio.models import BookingStatus, Engineer, Room, Service, StudioBooking
from apps.studio.money import net_paid
from core.models import AuditLog

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(django_user_model):
    return {
        'ama': django_user_model.objects.create_user(email='ama@x.test', password='p-Ass-1',
                                                     role='customer'),
        'kofi': django_user_model.objects.create_user(email='kofi@x.test', password='p-Ass-1',
                                                      role='customer'),
        'staff': django_user_model.objects.create_user(email='s@x.test', password='p-Ass-1',
                                                       role='admin'),
        'room': Room.objects.create(name='Studio One', capacity=30),
        'eng': Engineer.objects.create(name='Michael Kontor'),
        'svc': Service.objects.create(name='Rehearsal', base_price_pesewas=15000,
                                      duration_minutes=60),
    }


_slot = iter(range(1, 10_000))


def booking(w, who='ama', days=5, total=40000):
    start = (timezone.now() + timedelta(days=days, hours=next(_slot) * 3)).replace(
        minute=0, second=0, microsecond=0)
    return StudioBooking.objects.create(
        customer=w[who], room=w['room'], engineer=w['eng'], service=w['svc'],
        start_at=start, end_at=start + timedelta(hours=1), total_pesewas=total)


def cancelled_with_credit(w, paid=20000, days_left=30):
    b = booking(w, days=10)
    record_payment(b, paid, 'mobile_money', '', '', actor=w['staff'])
    StudioBooking.objects.filter(pk=b.pk).update(status=BookingStatus.CANCELLED)
    AuditLog.record('booking.cancelled', target=b, reference=b.reference,
                    credit_pesewas=paid,
                    credit_until=(timezone.localdate() + timedelta(days=days_left)).isoformat())
    b.refresh_from_db()
    return b


def staff_client(w):
    c = APIClient()
    c.force_authenticate(w['staff'])
    return c


class TestApply:
    def test_moves_the_deposit_and_confirms_the_new_booking(self, w):
        old = cancelled_with_credit(w)
        new = booking(w, days=12)
        txn = apply_credit(old, new, actor=w['staff'])
        new.refresh_from_db()
        assert txn.provider == Provider.CREDIT and txn.amount_pesewas == 20000
        assert net_paid(new) == 20000 and net_paid(old) == 0
        assert new.status == BookingStatus.CONFIRMED        # the deposit is in
        assert LedgerEntry.objects.get(transaction__booking=old,
                                       entry_type=EntryType.TRANSFER).amount_pesewas == 20000
        assert hasattr(txn, 'receipt')
        assert AuditLog.objects.filter(action='payment.credit_applied').count() == 2

    def test_never_more_than_is_owed(self, w):
        old = cancelled_with_credit(w, paid=30000)
        new = booking(w, days=12, total=20000)
        apply_credit(old, new, actor=w['staff'])
        assert outstanding(new) == 0
        assert net_paid(old) == 10000                       # the rest is still credit
        assert available_credits(w['ama'])[0].amount_pesewas == 10000

    def test_spent_credit_cannot_be_spent_twice(self, w):
        old = cancelled_with_credit(w)
        apply_credit(old, booking(w, days=12), actor=w['staff'])
        with pytest.raises(LedgerError, match='no credit left'):
            apply_credit(old, booking(w, days=13), actor=w['staff'])

    def test_only_for_the_same_customer(self, w):
        with pytest.raises(LedgerError, match='same customer'):
            apply_credit(cancelled_with_credit(w), booking(w, who='kofi'), actor=w['staff'])

    def test_expired_credit_is_refused(self, w):
        with pytest.raises(LedgerError, match='expired'):
            apply_credit(cancelled_with_credit(w, days_left=-1), booking(w), actor=w['staff'])

    def test_the_new_session_must_finish_inside_the_window(self, w):
        with pytest.raises(LedgerError, match='completed by'):
            apply_credit(cancelled_with_credit(w, days_left=3), booking(w, days=20),
                         actor=w['staff'])

    def test_a_cancellation_without_credit_gives_none(self, w):
        b = booking(w, days=10)
        record_payment(b, 20000, 'cash', '', '', actor=w['staff'])
        StudioBooking.objects.filter(pk=b.pk).update(status=BookingStatus.CANCELLED)
        b.refresh_from_db()
        with pytest.raises(LedgerError, match='did not keep'):
            apply_credit(b, booking(w), actor=w['staff'])
        assert available_credits(w['ama']) == []


class TestStaffApi:
    def test_detail_offers_the_credit_and_apply_uses_it(self, w):
        old = cancelled_with_credit(w)
        new = booking(w, days=12)
        c = staff_client(w)
        detail = c.get(f'/api/v1/admin/bookings/{new.pk}/').data
        assert detail['available_credits'][0]['reference'] == old.reference
        assert detail['available_credits'][0]['amount_display'] == 'GHS 200.00'
        res = c.post(f'/api/v1/admin/bookings/{new.pk}/apply-credit/',
                     {'source_booking': str(old.pk)}, format='json')
        assert res.status_code == 200
        assert res.data['available_credits'] == []
        assert res.data['status'] == BookingStatus.CONFIRMED

    def test_a_refusal_is_a_readable_400(self, w):
        old = cancelled_with_credit(w, days_left=-1)
        res = staff_client(w).post(f'/api/v1/admin/bookings/{booking(w).pk}/apply-credit/',
                                   {'source_booking': str(old.pk)}, format='json')
        assert res.status_code == 400 and 'expired' in res.data['detail']

    def test_staff_cancel_can_keep_the_deposit_as_credit(self, w):
        b = booking(w, days=10)
        record_payment(b, 20000, 'cash', '', '', actor=w['staff'])
        res = staff_client(w).post(f'/api/v1/admin/bookings/{b.pk}/cancel/',
                                   {'keep_as_credit': True}, format='json')
        assert res.status_code == 200
        assert available_credits(w['ama'])[0].reference == b.reference

    def test_staff_cancel_without_it_keeps_none(self, w):
        b = booking(w, days=10)
        record_payment(b, 20000, 'cash', '', '', actor=w['staff'])
        staff_client(w).post(f'/api/v1/admin/bookings/{b.pk}/cancel/', {}, format='json')
        assert available_credits(w['ama']) == []

    def test_customers_cannot_apply_credit(self, w):
        old = cancelled_with_credit(w)
        c = APIClient()
        c.force_authenticate(w['ama'])
        res = c.post(f'/api/v1/admin/bookings/{booking(w).pk}/apply-credit/',
                     {'source_booking': str(old.pk)}, format='json')
        assert res.status_code == 403


def test_the_payments_screen_shows_the_fee(w):
    b = booking(w)
    txn = Transaction.objects.create(customer=w['ama'], booking=b, provider=Provider.PAYSTACK,
                                     amount_pesewas=20000, fee_pesewas=398,
                                     commission_percent=0, provider_ref='R-1')
    row = staff_client(w).get('/api/v1/admin/payments/').data['results'][0]
    assert row['id'] == str(txn.pk)
    assert row['fee_display'] == 'GHS 3.98' and row['charged_display'] == 'GHS 203.98'
