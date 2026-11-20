"""
Booking needs an identity check SENT to ONA (BOOKING_REQUIRES_IDENTITY_CHECK).

One rule, read in four places that must agree: the booking gate (403), the
"must_verify_identity" flag the booking page's banner reads from /auth/me/,
the journey's "Verify your identity" step, and the staff booking detail.
"""

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.kyc.models import IdentityCheck, IdentityStatus, has_sent_check
from apps.studio.models import Engineer, Room, Service, StudioBooking

pytestmark = pytest.mark.django_db

BOOK = '/api/v1/studio/bookings/'
ME = '/api/v1/auth/me/'
JOURNEY = '/api/v1/auth/journey/'


@pytest.fixture
def gate(settings):
    """The gate as on staging and production: on, with a KYC service set up."""
    settings.BOOKING_REQUIRES_IDENTITY_CHECK = True
    settings.BOOKING_REQUIRES_VERIFIED_EMAIL = True
    settings.KYC_API_URL = 'https://kyc.test'
    settings.KYC_API_KEY = 'kyc_abcd1234_secret'
    return settings


@pytest.fixture
def ama(django_user_model):
    return django_user_model.objects.create_user(
        email='ama@x.test', password='p-Ass-1', role='customer', first_name='Ama',
        email_verified_at=timezone.now())


@pytest.fixture
def slot():
    start = datetime.combine(date.today() + timedelta(days=5), datetime.min.time(),
                             tzinfo=ZoneInfo('Africa/Accra')) + timedelta(hours=10)
    return {'room': str(Room.objects.create(name='A', hourly_rate_pesewas=100).pk),
            'engineer': str(Engineer.objects.create(name='E').pk),
            'service': str(Service.objects.create(name='S', base_price_pesewas=0,
                                                  duration_minutes=60).pk),
            'start_at': start.isoformat(), 'hours': 1}


def as_(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def check(user, status, reason='', ago=timedelta(0)):
    c = IdentityCheck.objects.create(verification_id=uuid.uuid4(), user=user, status=status,
                                     reason=reason)
    # created_at is auto_now_add: set it after, so "latest" is unambiguous.
    IdentityCheck.objects.filter(pk=c.pk).update(created_at=timezone.now() - ago)
    return c


def step(user):
    steps = as_(user).get(JOURNEY).data['steps']
    return next((s for s in steps if s['key'] == 'verify_identity'), None)


class TestWithoutACheck:
    def test_booking_is_refused_with_a_way_forward(self, gate, ama, slot):
        res = as_(ama).post(BOOK, slot, format='json')
        assert res.status_code == 403
        assert 'Verify your identity' in str(res.data)
        assert not StudioBooking.objects.exists()

    def test_the_banner_flag_and_the_journey_say_the_same(self, gate, ama):
        assert as_(ama).get(ME).data['must_verify_identity'] is True
        s = step(ama)
        assert s['done'] is False and s['href'] == '/customer/verify'
        assert s['cta'] == 'Verify your identity'

    def test_the_step_comes_right_after_the_email(self, gate, ama):
        keys = [s['key'] for s in as_(ama).get(JOURNEY).data['steps']]
        assert keys[:4] == ['account', 'verify_email', 'verify_identity', 'phone']

    def test_reading_is_never_gated(self, gate, ama):
        assert as_(ama).get(BOOK).status_code == 200

    def test_the_email_gate_still_answers_first(self, gate, django_user_model, slot):
        new = django_user_model.objects.create_user(email='n@x.test', password='p-Ass-1',
                                                    role='customer')
        res = as_(new).post(BOOK, slot, format='json')
        assert res.status_code == 403 and 'Confirm your email' in str(res.data)


@pytest.mark.parametrize('status', [IdentityStatus.SUBMITTED, IdentityStatus.IN_REVIEW,
                                    IdentityStatus.APPROVED])
def test_a_sent_or_approved_check_is_enough_to_book(gate, ama, slot, status):
    check(ama, status)
    assert as_(ama).post(BOOK, slot, format='json').status_code == 201
    assert as_(ama).get(ME).data['must_verify_identity'] is False
    s = step(ama)
    assert s['done'] is True
    assert s['body'].startswith('Verified' if status == IdentityStatus.APPROVED else 'Sent')


@pytest.mark.parametrize('status, cta, urgent', [
    (IdentityStatus.DRAFT, 'Finish your check', False),
    (IdentityStatus.CHANGES_REQUESTED, 'See what to change', True),
    (IdentityStatus.REJECTED, 'Start a new check', True),
])
def test_an_unsent_check_still_blocks_and_says_what_to_do(gate, ama, slot, status, cta, urgent):
    check(ama, status, reason='The photo of the back is blurred.')
    assert as_(ama).post(BOOK, slot, format='json').status_code == 403
    s = step(ama)
    assert s['done'] is False and s['cta'] == cta and bool(s.get('urgent')) is urgent
    if status != IdentityStatus.DRAFT:
        assert 'blurred' in s['body']  # the reviewer's note, written for the person


def test_the_latest_attempt_decides(gate, ama):
    check(ama, IdentityStatus.SUBMITTED, ago=timedelta(days=2))
    check(ama, IdentityStatus.CHANGES_REQUESTED)
    assert has_sent_check(ama) is False


def test_an_earlier_approval_keeps_counting(gate, ama):
    check(ama, IdentityStatus.APPROVED, ago=timedelta(days=30))
    check(ama, IdentityStatus.REJECTED)
    assert has_sent_check(ama) is True


class TestWhenTheGateDoesNotApply:
    def test_switched_off(self, gate, ama, slot):
        gate.BOOKING_REQUIRES_IDENTITY_CHECK = False
        assert as_(ama).post(BOOK, slot, format='json').status_code == 201
        assert as_(ama).get(ME).data['must_verify_identity'] is False
        assert step(ama) is None

    def test_no_kyc_service_means_no_gate(self, gate, ama, slot):
        # Otherwise nobody could ever book: there would be nowhere to send a check.
        gate.KYC_API_URL = ''
        assert as_(ama).post(BOOK, slot, format='json').status_code == 201
        assert step(ama) is None

    def test_production_default_is_on(self):
        from config.settings import base
        assert base.BOOKING_REQUIRES_IDENTITY_CHECK is True


class TestStaffSeeIt:
    def test_booking_detail_carries_the_customers_check(self, gate, ama, slot, django_user_model):
        staff = django_user_model.objects.create_user(email='s@x.test', password='p-Ass-1',
                                                      role='admin')
        c = check(ama, IdentityStatus.IN_REVIEW)
        booking = as_(ama).post(BOOK, slot, format='json').data
        detail = as_(staff).get(f'/api/v1/admin/bookings/{booking["id"]}/').data
        assert detail['customer_identity'] == {
            'verification_id': str(c.verification_id), 'status': 'in_review',
            'status_display': 'Being reviewed', 'verified': False}

    def test_no_check_at_all_is_none(self, gate, ama, slot, django_user_model):
        gate.BOOKING_REQUIRES_IDENTITY_CHECK = False
        staff = django_user_model.objects.create_user(email='s@x.test', password='p-Ass-1',
                                                      role='admin')
        booking = as_(ama).post(BOOK, slot, format='json').data
        detail = as_(staff).get(f'/api/v1/admin/bookings/{booking["id"]}/').data
        assert detail['customer_identity'] is None
