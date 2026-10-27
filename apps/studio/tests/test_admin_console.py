"""
Task 14 — the staff console API: overview, bookings with their lifecycle,
customers and payments. Staff only; every transition audited and emailed.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core import mail
from django.urls import reverse
from rest_framework.test import APIClient

from apps.payments import ledger
from apps.studio.models import BookingStatus, Engineer, Room, Service, StudioBooking
from core.models import AuditLog

pytestmark = pytest.mark.django_db

TZ = ZoneInfo('Africa/Accra')


def at(days, hour):
    d = date.today() + timedelta(days=days)
    return datetime.combine(d, datetime.min.time(), tzinfo=TZ) + timedelta(hours=hour)


@pytest.fixture
def world(django_user_model):
    U = django_user_model.objects
    return {
        'admin': U.create_user(email='staff@x.test', password='p-Ass-1', role='admin'),
        'ama': U.create_user(email='ama@x.test', password='p-Ass-1', role='customer',
                             first_name='Ama', last_name='Owusu', phone='0244000000'),
        'kofi': U.create_user(email='kofi@x.test', password='p-Ass-1', role='customer',
                              first_name='Kofi'),
        'pro': U.create_user(email='pro@x.test', password='p-Ass-1', role='professional'),
        'room': Room.objects.create(name='A', hourly_rate_pesewas=20000),
        'room_b': Room.objects.create(name='B', hourly_rate_pesewas=20000),
        'eng': Engineer.objects.create(name='One'),
        'eng_b': Engineer.objects.create(name='Two'),
        'svc': Service.objects.create(name='Rec', base_price_pesewas=1, duration_minutes=60),
    }


def booking(w, start, hours=2, who='ama', room='room', eng='eng', **kw):
    return StudioBooking.objects.create(
        customer=w[who], room=w[room], engineer=w[eng], service=w['svc'], start_at=start,
        end_at=start + timedelta(hours=hours), total_pesewas=20000 * hours, **kw)


def client(user=None):
    c = APIClient()
    if user is not None:
        c.force_authenticate(user)
    return c


@pytest.fixture
def staff(world):
    return client(world['admin'])


def u(name, *args):
    return reverse(f'adminapi:{name}', args=args)


class TestAccess:
    def routes(self, w):
        b = booking(w, at(5, 10))
        return [
            ('get', u('overview')),
            ('get', u('booking-list')), ('get', u('booking-detail', b.pk)),
            ('post', u('booking-confirm', b.pk)), ('post', u('booking-complete', b.pk)),
            ('post', u('booking-cancel', b.pk)),
            ('get', u('customer-list')), ('get', u('customer-detail', w['ama'].pk)),
            ('get', u('payment-list')),
        ]

    @pytest.mark.parametrize('who', ['ama', 'pro'])
    def test_non_staff_get_403_on_every_route(self, world, who):
        c = client(world[who])
        for method, path in self.routes(world):
            assert getattr(c, method)(path).status_code == 403, path
        # And nothing changed.
        assert not StudioBooking.objects.exclude(status=BookingStatus.PENDING).exists()

    def test_anonymous_gets_401(self, world):
        for method, path in self.routes(world):
            assert getattr(client(), method)(path).status_code == 401, path


class TestBookings:
    def test_list_carries_the_customer_and_what_was_paid(self, staff, world):
        b = booking(world, at(5, 10))
        row = staff.get(u('booking-list')).data['results'][0]
        assert row['reference'] == b.reference
        assert row['customer'] == {'id': str(world['ama'].pk), 'public_ref': world['ama'].public_ref,
                                   'name': 'Ama Owusu', 'email': 'ama@x.test', 'phone': '0244000000'}
        assert row['paid_pesewas'] == 0 and row['paid_display'] == 'GHS 0.00'
        assert row['allowed_actions'] == ['confirm', 'cancel']

    def test_sees_every_customer_and_searches_them(self, staff, world):
        mine = booking(world, at(5, 10))
        booking(world, at(6, 10), who='kofi')
        assert staff.get(u('booking-list')).data['count'] == 2
        res = staff.get(u('booking-list'), {'search': 'owusu'}).data
        assert [r['reference'] for r in res['results']] == [mine.reference]
        res = staff.get(u('booking-list'), {'search': mine.reference.lower()}).data
        assert res['count'] == 1

    def test_when_filters(self, staff, world):
        soon = booking(world, at(1, 10))
        later = booking(world, at(3, 10))
        past = booking(world, at(-2, 10), status=BookingStatus.COMPLETED)
        today = booking(world, at(0, 23), hours=1, room='room_b', eng='eng_b')

        def refs(when):
            return [r['reference'] for r in
                    staff.get(u('booking-list'), {'when': when}).data['results']]
        assert refs('upcoming')[-2:] == [soon.reference, later.reference]
        assert refs('past') == [past.reference]
        assert refs('today') == [today.reference]

    def test_status_filter(self, staff, world):
        booking(world, at(1, 10))
        c = booking(world, at(3, 10), status=BookingStatus.CONFIRMED)
        res = staff.get(u('booking-list'), {'status': 'confirmed'}).data
        assert [r['reference'] for r in res['results']] == [c.reference]


class TestLifecycle:
    def test_confirm_is_audited_and_emails_the_customer(
            self, staff, world, django_capture_on_commit_callbacks):
        b = booking(world, at(5, 10))
        with django_capture_on_commit_callbacks(execute=True):
            res = staff.post(u('booking-confirm', b.pk), {'reason': 'Called to check'})
        assert res.status_code == 200, res.data
        assert res.data['status'] == 'confirmed'
        assert res.data['allowed_actions'] == ['cancel']

        log = AuditLog.objects.get(action='booking.confirmed')
        assert log.actor == world['admin'] and log.target_id == str(b.pk)
        assert log.metadata['previous_status'] == 'pending'
        assert log.metadata['reason'] == 'Called to check'
        assert [m.to for m in mail.outbox] == [['ama@x.test']]
        assert mail.outbox[0].subject == f'Session confirmed — {b.reference}'

        # The detail shows it in the history.
        detail = staff.get(u('booking-detail', b.pk)).data
        assert [h['action'] for h in detail['history']] == ['booking.confirmed']
        assert detail['history'][0]['actor_email'] == 'staff@x.test'

    def test_confirm_refuses_anything_but_a_live_pending_booking(self, staff, world):
        done = booking(world, at(-3, 10), status=BookingStatus.COMPLETED)
        ended = booking(world, at(-3, 14))
        for b in (done, ended):
            res = staff.post(u('booking-confirm', b.pk))
            assert res.status_code == 400
            assert 'pending' in res.data['detail']
        assert not AuditLog.objects.filter(action='booking.confirmed').exists()

    def test_complete_only_once_the_session_has_started(
            self, staff, world, django_capture_on_commit_callbacks):
        future = booking(world, at(5, 10), status=BookingStatus.CONFIRMED)
        assert staff.post(u('booking-complete', future.pk)).status_code == 400

        past = booking(world, at(-1, 10), status=BookingStatus.CONFIRMED)
        with django_capture_on_commit_callbacks(execute=True):
            res = staff.post(u('booking-complete', past.pk))
        assert res.status_code == 200 and res.data['status'] == 'completed'
        assert res.data['allowed_actions'] == []
        assert AuditLog.objects.filter(action='booking.completed', actor=world['admin']).exists()
        assert mail.outbox[0].subject == f'Thanks for the session — {past.reference}'

    def test_staff_can_cancel_a_no_show_the_customer_no_longer_can(
            self, staff, world, django_capture_on_commit_callbacks):
        started = booking(world, at(-1, 10), status=BookingStatus.CONFIRMED)
        assert not started.can_change  # the customer's own rule
        with django_capture_on_commit_callbacks(execute=True):
            res = staff.post(u('booking-cancel', started.pk), {'reason': 'No-show'})
        assert res.status_code == 200 and res.data['status'] == 'cancelled'
        log = AuditLog.objects.get(action='booking.cancelled')
        assert log.metadata['by_staff'] is True and log.metadata['paid_pesewas'] == 0
        assert mail.outbox[0].subject == f'Booking cancelled — {started.reference}'

        # Cancelled is final.
        assert staff.post(u('booking-cancel', started.pk)).status_code == 400

    def test_a_cancelled_slot_is_free_again(self, staff, world):
        b = booking(world, at(5, 10))
        staff.post(u('booking-cancel', b.pk))
        booking(world, at(5, 10), who='kofi')  # would violate the exclusion constraint

    def test_unknown_booking_is_404(self, staff):
        assert staff.post(u('booking-confirm', '00000000-0000-0000-0000-000000000000')
                          ).status_code == 404


class TestMoney:
    @pytest.fixture(autouse=True)
    def commission(self, settings):
        settings.STUDIO_COMMISSION_PERCENT = 10

    @pytest.fixture
    def paid(self, world):
        b = booking(world, at(-1, 10), status=BookingStatus.COMPLETED)
        txn = ledger.open_transaction(b)
        ledger.mark_succeeded(txn, provider_ref='HUB-1')
        ledger.release(txn, actor=world['admin'])
        return b, txn

    def test_booking_detail_shows_payment_ledger_and_commission(self, staff, paid):
        b, txn = paid
        detail = staff.get(u('booking-detail', b.pk)).data
        assert detail['paid_pesewas'] == 40000 and detail['paid_display'] == 'GHS 400.00'
        [t] = detail['transactions']
        assert t['provider'] == 'paystack' and t['status'] == 'succeeded'
        assert t['commission_percent'] == 10
        assert t['commission_pesewas'] == 4000 and t['held_pesewas'] == 0
        assert t['held_display'] == 'GHS 0.00'
        assert [e['entry_type'] for e in t['ledger']] == ['hold', 'release']
        assert t['ledger'][1]['commission'] == {'percent': 10, 'amount_pesewas': 4000,
                                               'amount_display': 'GHS 40.00'}
        assert t['ledger'][1]['created_by_email'] == 'staff@x.test'

    def test_payments_list_and_filters(self, staff, paid, world):
        b, txn = paid
        rows = staff.get(u('payment-list')).data['results']
        assert [r['booking']['reference'] for r in rows] == [b.reference]
        assert rows[0]['customer']['email'] == 'ama@x.test'
        assert staff.get(u('payment-list'), {'status': 'failed'}).data['count'] == 0
        assert staff.get(u('payment-list'), {'search': b.reference}).data['count'] == 1

    def test_paid_amount_is_net_of_refunds_without_double_counting(self, staff, world):
        b = booking(world, at(-1, 10), status=BookingStatus.COMPLETED)
        for ref in ('HUB-A', 'HUB-B'):  # two payments, each with two ledger rows
            t = ledger.open_transaction(b)
            ledger.mark_succeeded(t, provider_ref=ref)
        ledger.refund(t, 5000, actor=world['admin'], note='Part refund')
        row = staff.get(u('booking-list')).data['results'][0]
        assert row['paid_pesewas'] == 80000 and row['refunded_pesewas'] == 5000
        assert row['paid_display'] == 'GHS 750.00'

    def test_overview_numbers_are_real(self, staff, world, paid):
        booking(world, at(0, 23), hours=1, room='room_b', eng='eng_b')          # today, pending
        booking(world, at(2, 10), status=BookingStatus.CONFIRMED)              # this week
        booking(world, at(3, 10), room='room_b', status=BookingStatus.CANCELLED)  # ignored
        data = staff.get(u('overview')).data
        assert data['today_count'] == 1
        assert data['pending_count'] == 1
        assert data['week_count'] >= 1 and data['week_hours'] >= 2
        assert data['customers_total'] == 2 and data['customers_new_30d'] == 2
        assert data['received_30d_pesewas'] == 40000
        assert data['received_30d_display'] == 'GHS 400.00'
        assert data['commission_30d_pesewas'] == 4000
        assert len(data['needs_confirmation']) == 1
        assert data['needs_confirmation'][0]['customer']['name'] == 'Ama Owusu'
        assert len(data['today']) == 1


class TestCustomers:
    def test_directory_with_counts_and_search(self, staff, world):
        booking(world, at(5, 10))
        booking(world, at(-5, 10), status=BookingStatus.COMPLETED)
        booking(world, at(6, 10), status=BookingStatus.CANCELLED)
        rows = {r['email']: r for r in staff.get(u('customer-list')).data['results']}
        assert set(rows) == {'ama@x.test', 'kofi@x.test'}  # no staff or professionals
        ama = rows['ama@x.test']
        assert ama['name'] == 'Ama Owusu' and ama['phone'] == '0244000000'
        assert ama['booking_count'] == 3 and ama['upcoming_count'] == 1
        assert ama['paid_display'] == 'GHS 0.00'
        assert rows['kofi@x.test']['booking_count'] == 0

        res = staff.get(u('customer-list'), {'search': 'kofi'}).data
        assert [r['email'] for r in res['results']] == ['kofi@x.test']

    def test_detail(self, staff, world):
        res = staff.get(u('customer-detail', world['ama'].pk))
        assert res.status_code == 200 and res.data['first_name'] == 'Ama'

    def test_a_professional_is_not_a_customer(self, staff, world):
        assert staff.get(u('customer-detail', world['pro'].pk)).status_code == 404
