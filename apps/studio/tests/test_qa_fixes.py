"""
Regression tests for the 2026-10-26 QA audit (docs/qa/uat-v0.1.md, F1–F7).
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import serializers
from rest_framework.test import APIClient, APIRequestFactory

from apps.marketplace.models import Professional, RateType
from apps.payments import ledger
from apps.payments.manual import LedgerError, record_payment
from apps.payments.models import Invoice
from apps.studio.models import BookingStatus, Engineer, Room, Service, StudioBooking
from apps.studio.serializers import BookingCreateSerializer
from core import lockout
from core.models import AuditLog

pytestmark = pytest.mark.django_db
TZ = ZoneInfo('Africa/Accra')


def at(days, hour):
    d = date.today() + timedelta(days=days)
    return datetime.combine(d, datetime.min.time(), tzinfo=TZ) + timedelta(hours=hour)


@pytest.fixture
def w(django_user_model):
    U = django_user_model.objects
    room = Room.objects.create(name='A', hourly_rate_pesewas=20000)
    return {
        'admin': U.create_user(email='staff@x.test', password='p-Ass-1', role='admin'),
        'ama': U.create_user(email='ama@x.test', password='Right-Passw0rd!', role='customer',
                             first_name='Ama', last_name='Owusu'),
        'room': room, 'eng': Engineer.objects.create(name='One'),
        'svc': Service.objects.create(name='Rec', base_price_pesewas=1, duration_minutes=60),
    }


def make(w, start, **kw):
    return StudioBooking.objects.create(
        customer=w['ama'], room=w['room'], engineer=w['eng'], service=w['svc'], start_at=start,
        end_at=start + timedelta(hours=2), total_pesewas=40000, **kw)


def as_user(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


class TestF1PaidBookingsTellTheTruth:
    def test_customer_api_shows_net_paid_and_cancel_records_and_says_it(
            self, w, django_capture_on_commit_callbacks):
        b = make(w, at(5, 10))
        record_payment(b, 15000, 'cash', '', '', actor=w['admin'])
        txn = b.transactions.get()
        ledger.refund(txn, 5000, actor=w['admin'], note='part')

        row = as_user(w['ama']).get(reverse('studio:booking-list'), {'when': 'upcoming'}).data['results'][0]
        assert row['paid_pesewas'] == 10000 and row['paid_display'] == 'GHS 100.00'
        summary = as_user(w['ama']).get(reverse('studio:booking-summary')).data
        assert summary['next']['paid_display'] == 'GHS 100.00'

        mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            res = as_user(w['ama']).post(reverse('studio:booking-cancel', args=[b.pk]))
        assert res.status_code == 200
        assert AuditLog.objects.get(action='booking.cancelled').metadata['paid_pesewas'] == 10000
        assert 'ONA is holding the GHS 100.00 you paid' in mail.outbox[0].body

    def test_an_unpaid_cancel_email_says_nothing_about_money(
            self, w, django_capture_on_commit_callbacks):
        b = make(w, at(5, 10))
        with django_capture_on_commit_callbacks(execute=True):
            as_user(w['ama']).post(reverse('studio:booking-cancel', args=[b.pk]))
        assert 'holding' not in mail.outbox[0].body


class TestF2RealNamesStayWithOna:
    def test_no_stage_name_means_a_neutral_label_everywhere(
            self, w, django_capture_on_commit_callbacks):
        pro = Professional.objects.create(name='Real Person', stage_name='', skills=['keys'],
                                          rate_type=RateType.NEGOTIABLE)
        b = make(w, at(5, 10))
        with django_capture_on_commit_callbacks(execute=True):
            res = as_user(w['admin']).post(reverse('adminapi:booking-assign', args=[b.pk]),
                                           {'professional': str(pro.pk),
                                            'professional_fee_pesewas': 5000}, format='json')
        assert res.status_code == 200
        mine = as_user(w['ama']).get(reverse('studio:booking-detail', args=[b.pk])).data
        assert mine['professional']['display_name'] == 'ONA session professional'
        assert 'Real Person' not in str(mine)
        assert 'Real Person' not in mail.outbox[0].body
        assert all('Real Person' not in line['description'] for line in Invoice.objects.get(booking=b).lines)


class TestF3PendingCapUnderRace:
    def test_the_cap_is_rechecked_at_save(self, w):
        for i in range(2):
            make(w, at(5 + i, 10))
        request = APIRequestFactory().post('/')
        request.user = w['ama']
        ser = BookingCreateSerializer(data={
            'room': str(w['room'].pk), 'engineer': str(w['eng'].pk), 'service': str(w['svc'].pk),
            'start_at': at(9, 10).isoformat(), 'hours': 1}, context={'request': request})
        assert ser.is_valid(), ser.errors   # two pending: passes validation
        make(w, at(8, 10))                  # a parallel request wins the third place
        with pytest.raises(serializers.ValidationError):
            ser.save()
        assert StudioBooking.objects.filter(status=BookingStatus.PENDING).count() == 3


class TestF4ResetUnlocks:
    def test_a_password_reset_lifts_the_lockout(self, w):
        cache.clear()
        for _ in range(lockout.LOCKOUT_ATTEMPTS):
            APIClient().post(reverse('accounts:login'), {'email': 'ama@x.test', 'password': 'x'},
                             format='json')
        assert lockout.is_locked('ama@x.test')
        APIClient().post(reverse('accounts:password-reset-confirm'), {
            'uid': urlsafe_base64_encode(force_bytes(w['ama'].pk)),
            'token': default_token_generator.make_token(w['ama']),
            'password': 'A-new-Passw0rd!', 'password_confirm': 'A-new-Passw0rd!'}, format='json')
        res = APIClient().post(reverse('accounts:login'),
                               {'email': 'ama@x.test', 'password': 'A-new-Passw0rd!'}, format='json')
        assert res.status_code == 200


class TestF5CustomerPaidIsNet:
    def test_directory_paid_is_net_of_refunds(self, w):
        b = make(w, at(5, 10))
        record_payment(b, 36200, 'cash', '', '', actor=w['admin'])
        ledger.refund(b.transactions.get(), 24001, actor=w['admin'], note='x')
        rows = as_user(w['admin']).get(reverse('adminapi:customer-list')).data['results']
        ama = next(r for r in rows if r['email'] == 'ama@x.test')
        assert ama['paid_pesewas'] == 12199 and ama['paid_display'] == 'GHS 121.99'


class TestF6NoRefundPromise:
    def test_payment_email_promises_no_refund(self, w, django_capture_on_commit_callbacks):
        b = make(w, at(5, 10))
        with django_capture_on_commit_callbacks(execute=True):
            record_payment(b, 1000, 'cash', '', '', actor=w['admin'])
        body = next(m.body for m in mail.outbox if m.subject.startswith('Payment received'))
        assert 'refund' not in body.lower()


class TestF7ReferenceCase:
    def test_the_same_reference_in_another_case_is_refused(self, w):
        b = make(w, at(5, 10))
        record_payment(b, 1000, 'mobile_money', 'QA-REF-X', '', actor=w['admin'])
        with pytest.raises(LedgerError, match='already been recorded'):
            record_payment(b, 1000, 'mobile_money', 'qa-ref-x', '', actor=w['admin'])
