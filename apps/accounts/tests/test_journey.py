"""
The customer journey: each step ticks itself off from real records; staff see
the same stage, new customers, a first visit, and the launch checklist.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.payments.manual import record_payment
from apps.studio.models import BookingStatus, Engineer, Room, Service, StudioBooking

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(django_user_model):
    return {
        'ama': django_user_model.objects.create_user(email='ama@x.test', password='p-Ass-1',
                                                     role='customer', first_name='Ama'),
        'staff': django_user_model.objects.create_user(email='s@x.test', password='p-Ass-1',
                                                       role='admin'),
        'room': Room.objects.create(name='Studio One', capacity=30),
        'fresh': django_user_model.objects.create_user(email='new@x.test', password='p-Ass-1',
                                                       role='customer'),
        'eng': Engineer.objects.create(name='Michael Kontor'),
        'svc': Service.objects.create(name='Rehearsal', base_price_pesewas=15000,
                                      duration_minutes=60),
    }


@pytest.fixture(autouse=True)
def ama_verified(w):
    w['ama'].email_verified_at = timezone.now()
    w['ama'].save(update_fields=['email_verified_at'])


def as_(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


_n = iter(range(1, 999))


def booking(w, days=5, **kw):
    start = (timezone.now() + timedelta(days=days, hours=next(_n) * 3)).replace(
        minute=0, second=0, microsecond=0)
    return StudioBooking.objects.create(
        customer=w['ama'], room=w['room'], engineer=w['eng'], service=w['svc'],
        start_at=start, end_at=start + timedelta(hours=1), total_pesewas=30000, **kw)


def steps(res):
    return {s['key']: s['done'] for s in res.data['steps']}


class TestCustomerGuide:
    def test_an_unverified_customer_is_asked_to_confirm_their_email_first(self, w):
        res = as_(w['fresh']).get('/api/v1/auth/journey/')
        assert res.data['next'] == 'verify_email'
        verify = next(s for s in res.data['steps'] if s['key'] == 'verify_email')
        assert verify['action'] == 'resend_verification'

    def test_a_new_customer_starts_at_the_beginning(self, w):
        res = as_(w['ama']).get('/api/v1/auth/journey/')
        assert res.status_code == 200
        assert steps(res)['account'] and not steps(res)['first_booking']
        assert res.data['next'] == 'phone'                   # email verified on create
        assert res.data['stage'] == 'signed_up' and res.data['is_new']

    def test_steps_tick_themselves_off_wherever_they_happened(self, w):
        w['ama'].phone = '0244000000'
        w['ama'].save()
        b = booking(w)
        res = as_(w['ama']).get('/api/v1/auth/journey/')
        assert steps(res)['phone'] and steps(res)['first_booking'] and not steps(res)['deposit']
        assert res.data['next'] == 'deposit' and res.data['stage'] == 'booked'
        record_payment(b, 15000, 'cash', '', '', actor=w['staff'])   # paid at the desk
        res = as_(w['ama']).get('/api/v1/auth/journey/')
        assert steps(res)['deposit'] and res.data['stage'] == 'paid'
        assert 'See you on' in res.data['steps'][-1]['body']

    def test_complete_after_the_first_session(self, w):
        w['ama'].phone = '0244000000'
        w['ama'].save()
        b = booking(w, days=-3, status=BookingStatus.COMPLETED)
        from apps.payments.models import Transaction
        Transaction.objects.create(customer=w['ama'], booking=b, provider='manual',
                                   amount_pesewas=30000, status='succeeded', commission_percent=0)
        res = as_(w['ama']).get('/api/v1/auth/journey/')
        assert res.data['complete'] and res.data['stage'] == 'came_in'

    def test_hiding_the_guide_is_remembered_and_reversible(self, w):
        c = as_(w['ama'])
        assert c.post('/api/v1/auth/journey/', {'hidden': True}, format='json').data['dismissed']
        assert c.get('/api/v1/auth/journey/').data['dismissed']
        assert not c.post('/api/v1/auth/journey/', {'hidden': False}, format='json').data['dismissed']

    def test_anonymous_gets_nothing(self):
        assert APIClient().get('/api/v1/auth/journey/').status_code == 401


class TestStaffSide:
    def test_customers_list_marks_new_customers_and_their_stage(self, w):
        booking(w)
        row = next(r for r in as_(w['staff']).get('/api/v1/admin/customers/').data['results']
                   if r['email'] == 'ama@x.test')
        assert row['is_new'] and row['stage'] == 'booked' and row['stage_display'] == 'Booked'

    def test_an_old_customer_is_not_new(self, w):
        type(w['ama']).objects.filter(pk=w['ama'].pk).update(
            date_joined=timezone.now() - timedelta(days=40))
        row = next(r for r in as_(w['staff']).get('/api/v1/admin/customers/').data['results']
                   if r['email'] == 'ama@x.test')
        assert not row['is_new']

    def test_the_first_booking_is_marked_and_later_ones_are_not(self, w):
        first, second = booking(w), booking(w, days=9)
        c = as_(w['staff'])
        assert c.get(f'/api/v1/admin/bookings/{first.pk}/').data['is_first_booking'] is True
        assert c.get(f'/api/v1/admin/bookings/{second.pk}/').data['is_first_booking'] is False

    def test_overview_lists_new_customers(self, w):
        data = as_(w['staff']).get('/api/v1/admin/overview/').data
        ama = next(c for c in data['new_customers'] if c['email'] == 'ama@x.test')
        assert ama['stage_display'] == 'Signed up'
        assert 's@x.test' not in [c['email'] for c in data['new_customers']]   # staff are not customers

    def test_setup_checklist_is_a_fixed_list_ticked_by_launch_check(self, w, settings):
        settings.PAYSTACK_SECRET_KEY = ''
        data = as_(w['staff']).get('/api/v1/admin/setup/').data
        items = {i['key']: i for i in data['items']}
        assert data['total'] == len(items) == 10
        assert items['catalogue']['done'] and items['catalogue']['href'] == '/admin/catalogue'
        assert not items['paystack']['done'] and 'PAYSTACK_SECRET_KEY' in items['paystack']['body']
        settings.PAYSTACK_SECRET_KEY = 'sk_live_x'
        items = {i['key']: i for i in as_(w['staff']).get('/api/v1/admin/setup/').data['items']}
        assert items['paystack']['done']

    def test_placeholder_catalogue_is_a_blocker_on_the_guide(self, w):
        Room.objects.create(name='Studio A', description='PLACEHOLDER.')
        items = {i['key']: i for i in as_(w['staff']).get('/api/v1/admin/setup/').data['items']}
        assert items['catalogue']['level'] == 'blocker' and not items['catalogue']['done']

    def test_customers_cannot_see_the_setup_checklist(self, w):
        assert as_(w['ama']).get('/api/v1/admin/setup/').status_code == 403


class TestWelcomeAndTours:
    def test_the_welcome_question_is_kept_and_shown_to_staff(self, w):
        c = as_(w['ama'])
        res = c.post('/api/v1/auth/journey/', {'intent': 'mix', 'welcomed': True}, format='json')
        assert res.data['intent'] == 'mix' and res.data['welcomed']
        row = next(r for r in as_(w['staff']).get('/api/v1/admin/customers/').data['results']
                   if r['email'] == 'ama@x.test')
        assert row['intent_display'] == 'Mix & master'

    def test_an_unknown_intent_is_ignored(self, w):
        res = as_(w['ama']).post('/api/v1/auth/journey/', {'intent': '<script>'}, format='json')
        assert res.data['intent'] == ''

    def test_tours_are_remembered_once(self, w):
        c = as_(w['ama'])
        c.post('/api/v1/auth/journey/', {'tour_seen': 'booking'}, format='json')
        res = c.post('/api/v1/auth/journey/', {'tour_seen': 'booking'}, format='json')
        assert res.data['tours_seen'] == ['booking']
        assert c.post('/api/v1/auth/journey/', {'tour_seen': 'nope'}, format='json').data['tours_seen'] == ['booking']

    def test_overview_funnel_is_cumulative(self, w):
        booking(w)
        stages = {s['key']: s['reached'] for s in
                  as_(w['staff']).get('/api/v1/admin/overview/').data['funnel']['stages']}
        assert stages['signed_up'] >= stages['booked'] >= stages['paid'] >= stages['came_in']
        assert stages['booked'] >= 1


def test_setup_checklist_collapses_placeholder_lines_into_one_sentence():
    from apps.studio.staff import StaffSetupView
    body = StaffSetupView._summary([
        'Placeholder text on room "Studio A" - replace it in Studio & prices, or switch it off.',
        'Placeholder text on room "Mix Suite" - replace it in Studio & prices, or switch it off.',
        'Placeholder text on service "Mixing" - replace it in Studio & prices, or switch it off.',
    ])
    assert body == 'Still placeholder text: rooms Studio A, Mix Suite; service Mixing. Replace each one, or switch it off.'
