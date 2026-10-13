"""
Task 12 — what the customer dashboard needs from the API: upcoming/past
lists, a summary, cancel, and reschedule that respects the double-booking rule.
"""

from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

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
        'me': U.create_user(email='me@x.test', password='p-Ass-1', role='customer'),
        'other': U.create_user(email='o@x.test', password='p-Ass-1', role='customer'),
        'room': Room.objects.create(name='A', hourly_rate_pesewas=20000),
        'eng': Engineer.objects.create(name='One'),
        'eng_2': Engineer.objects.create(name='Two'),
        'svc': Service.objects.create(name='Rec', base_price_pesewas=1, duration_minutes=60),
    }


def booking(w, start, hours=2, who='me', **kw):
    kw.setdefault('engineer', w['eng'])
    return StudioBooking.objects.create(
        customer=w[who], room=w['room'], service=w['svc'], start_at=start,
        end_at=start + timedelta(hours=hours), total_pesewas=20000 * hours, **kw)


@pytest.fixture
def api(world):
    c = APIClient()
    c.force_authenticate(world['me'])
    return c


def url(name, *args):
    return reverse(f'studio:booking-{name}', args=args)


class TestLists:
    def test_upcoming_is_soonest_first_and_excludes_finished_and_cancelled(self, api, world):
        later = booking(world, at(10, 10))
        sooner = booking(world, at(5, 10))
        booking(world, at(6, 10), status=BookingStatus.CANCELLED)
        booking(world, at(-3, 10), status=BookingStatus.COMPLETED)
        refs = [b['reference'] for b in api.get(url('list'), {'when': 'upcoming'}).data['results']]
        assert refs == [sooner.reference, later.reference]

    def test_past_holds_everything_else_newest_first(self, api, world):
        booking(world, at(5, 10))
        cancelled = booking(world, at(6, 10), status=BookingStatus.CANCELLED)
        done = booking(world, at(-3, 10), status=BookingStatus.COMPLETED)
        refs = [b['reference'] for b in api.get(url('list'), {'when': 'past'}).data['results']]
        assert refs == [cancelled.reference, done.reference]

    def test_never_shows_another_customers_booking(self, api, world):
        booking(world, at(5, 10), who='other')
        assert api.get(url('list'), {'when': 'upcoming'}).data['count'] == 0

    def test_can_change_flag(self, api, world):
        b = booking(world, at(5, 10))
        booking(world, at(-3, 10), status=BookingStatus.COMPLETED)
        rows = {r['reference']: r for r in api.get(url('list')).data['results']}
        assert rows[b.reference]['can_change'] is True
        assert [r for r in rows.values() if r['reference'] != b.reference][0]['can_change'] is False


class TestSummary:
    def test_counts_and_next(self, api, world):
        booking(world, at(10, 10), hours=3, status=BookingStatus.CONFIRMED)
        nxt = booking(world, at(5, 10), hours=2)
        booking(world, at(6, 10), status=BookingStatus.CANCELLED)
        data = api.get(url('summary')).data
        assert data['upcoming_count'] == 2
        assert data['pending_count'] == 1
        assert data['upcoming_hours'] == 5
        assert data['next']['reference'] == nxt.reference

    def test_empty(self, api, world):
        data = api.get(url('summary')).data
        assert data == {'upcoming_count': 0, 'pending_count': 0, 'upcoming_hours': 0, 'next': None}

    def test_requires_login(self, world):
        assert APIClient().get(url('summary')).status_code == 401


class TestCancel:
    def test_cancel_releases_the_slot_and_is_audited(self, api, world):
        b = booking(world, at(5, 10))
        res = api.post(url('cancel', b.id))
        assert res.status_code == 200 and res.data['status'] == 'cancelled'
        booking(world, at(5, 10), who='other')  # slot is free again
        assert AuditLog.objects.filter(action='booking.cancelled', actor=world['me']).exists()

    def test_cannot_cancel_a_past_or_cancelled_booking(self, api, world):
        past = booking(world, at(-2, 10), status=BookingStatus.CONFIRMED)
        assert api.post(url('cancel', past.id)).status_code == 400
        gone = booking(world, at(5, 10), status=BookingStatus.CANCELLED)
        assert api.post(url('cancel', gone.id)).status_code == 400

    def test_cannot_cancel_someone_elses(self, api, world):
        theirs = booking(world, at(5, 10), who='other')
        assert api.post(url('cancel', theirs.id)).status_code == 404
        theirs.refresh_from_db()
        assert theirs.status == BookingStatus.PENDING


class TestReschedule:
    def test_moves_the_session_keeping_length_and_price(self, api, world):
        b = booking(world, at(5, 10), hours=2)
        res = api.post(url('reschedule', b.id), {'start_at': at(5, 14).isoformat()}, format='json')
        assert res.status_code == 200, res.data
        b.refresh_from_db()
        assert b.start_at == at(5, 14) and b.end_at == at(5, 16)
        assert b.total_pesewas == 40000
        assert AuditLog.objects.filter(action='booking.rescheduled').exists()

    def test_overlapping_its_own_old_slot_is_fine(self, api, world):
        b = booking(world, at(5, 10), hours=2)                       # 10–12
        res = api.post(url('reschedule', b.id), {'start_at': at(5, 11).isoformat()}, format='json')
        assert res.status_code == 200, res.data                      # 11–13

    def test_cannot_move_onto_someone_elses_slot(self, api, world):
        booking(world, at(5, 14), who='other', engineer=world['eng_2'])
        b = booking(world, at(5, 10))
        res = api.post(url('reschedule', b.id), {'start_at': at(5, 13).isoformat()}, format='json')
        assert res.status_code == 400 and 'booked' in str(res.data)

    def test_race_lost_to_the_constraint_is_a_409(self, api, world):
        booking(world, at(5, 14), who='other', engineer=world['eng_2'],
                status=BookingStatus.CONFIRMED)
        b = booking(world, at(5, 10))
        with mock.patch('apps.studio.serializers.is_free', return_value=(True, '')):
            res = api.post(url('reschedule', b.id), {'start_at': at(5, 14).isoformat()},
                           format='json')
        assert res.status_code == 409
        b.refresh_from_db()
        assert b.start_at == at(5, 10)

    def test_same_rules_as_booking(self, api, world):
        b = booking(world, at(5, 10), hours=2)
        bad = [at(5, 20),                       # runs past closing
               at(-1, 10),                      # in the past
               at(5, 10) + timedelta(minutes=30)]  # off the hour
        for start in bad:
            res = api.post(url('reschedule', b.id), {'start_at': start.isoformat()}, format='json')
            assert res.status_code == 400, start

    def test_cannot_move_a_cancelled_booking(self, api, world):
        b = booking(world, at(5, 10), status=BookingStatus.CANCELLED)
        res = api.post(url('reschedule', b.id), {'start_at': at(6, 10).isoformat()}, format='json')
        assert res.status_code == 400
