"""
POST /studio/bookings/ — the Phase 1 exit slice, API half.

A conflicting booking must come back as a clean 4xx with a sentence, never
a 500 — including when the conflict is only caught by the database.
"""

from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.studio.models import BookingStatus, Engineer, Room, Service, StudioBooking

pytestmark = pytest.mark.django_db

TZ = ZoneInfo('Africa/Accra')


def next_monday_at(hour):
    today = datetime.now(TZ).date()
    monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    return datetime.combine(monday, datetime.min.time(), tzinfo=TZ) + timedelta(hours=hour)


@pytest.fixture
def world(django_user_model):
    U = django_user_model.objects
    return {
        'customer': U.create_user(email='c@x.test', password='p-Ass-1', role='customer'),
        'other': U.create_user(email='o@x.test', password='p-Ass-1', role='customer'),
        'pro': U.create_user(email='p@x.test', password='p-Ass-1', role='professional'),
        'admin': U.create_superuser(email='a@x.test', password='p-Ass-1'),
        'room': Room.objects.create(name='A', hourly_rate_pesewas=20000),
        'eng': Engineer.objects.create(name='One'),
        'eng_2': Engineer.objects.create(name='Two'),
        'svc': Service.objects.create(name='Rec', base_price_pesewas=20000, duration_minutes=60),
    }


@pytest.fixture
def api():
    return APIClient()


def payload(w, **over):
    base = {'room': w['room'].id, 'engineer': w['eng'].id, 'service': w['svc'].id,
            'start_at': next_monday_at(10).isoformat(), 'hours': 4, 'notes': 'Bring a cajon'}
    base.update(over)
    return base


LIST = lambda: reverse('studio:booking-list')  # noqa: E731


class TestCreate:
    def test_customer_books_and_gets_reference_and_frozen_total(self, api, world):
        api.force_authenticate(world['customer'])
        res = api.post(LIST(), payload(world), format='json')
        assert res.status_code == 201, res.data
        assert res.data['reference'].startswith('ONA-')
        assert res.data['status'] == 'pending'
        assert res.data['total_pesewas'] == 80000           # 200/hr × 4hr — the design's example
        assert res.data['total_display'] == 'GHS 800.00'
        assert res.data['duration_minutes'] == 240
        assert res.data['room']['name'] == 'A'
        booking = StudioBooking.objects.get(reference=res.data['reference'])
        assert booking.customer == world['customer']
        assert booking.end_at == booking.start_at + timedelta(hours=4)

    def test_anonymous_is_401(self, api, world):
        assert api.post(LIST(), payload(world), format='json').status_code == 401

    @pytest.mark.parametrize('who', ['pro', 'admin'])
    def test_only_customers_create(self, api, world, who):
        api.force_authenticate(world[who])
        assert api.post(LIST(), payload(world), format='json').status_code == 403


class TestValidation:
    def setup_method(self):
        self.api = APIClient()

    def post(self, w, **over):
        self.api.force_authenticate(w['customer'])
        return self.api.post(LIST(), payload(w, **over), format='json')

    def test_overlap_is_a_clean_400_with_a_sentence(self, world):
        assert self.post(world).status_code == 201
        res = self.post(world, engineer=world['eng_2'].id, start_at=next_monday_at(12).isoformat())
        assert res.status_code == 400
        assert 'room' in str(res.data).lower() and 'booked' in str(res.data).lower()

    def test_engineer_clash_names_the_engineer(self, world):
        self.post(world)
        other_room = Room.objects.create(name='B', hourly_rate_pesewas=1)
        res = self.post(world, room=other_room.id)
        assert res.status_code == 400 and 'engineer' in str(res.data).lower()

    def test_past_time_rejected(self, world):
        yesterday_10 = (datetime.now(TZ) - timedelta(days=1)).replace(hour=10, minute=0, second=0,
                                                                    microsecond=0)
        res = self.post(world, start_at=yesterday_10.isoformat())
        assert res.status_code == 400 and 'passed' in str(res.data).lower()

    def test_off_the_hour_rejected(self, world):
        res = self.post(world, start_at=(next_monday_at(10) + timedelta(minutes=30)).isoformat())
        assert res.status_code == 400 and 'on the hour' in str(res.data).lower()

    def test_outside_opening_hours_rejected(self, world):
        assert self.post(world, start_at=next_monday_at(7).isoformat()).status_code == 400
        assert self.post(world, start_at=next_monday_at(21).isoformat()).status_code == 400

    def test_running_past_closing_rejected(self, world):
        res = self.post(world, start_at=next_monday_at(19).isoformat(), hours=3)
        assert res.status_code == 400 and 'closing' in str(res.data).lower()

    def test_inactive_room_rejected(self, world):
        world['room'].is_active = False
        world['room'].save()
        assert self.post(world).status_code == 400

    def test_hours_bounds(self, world):
        assert self.post(world, hours=0).status_code == 400
        assert self.post(world, hours=13).status_code == 400


class TestRace:
    def test_constraint_violation_is_a_409_not_a_500(self, api, world):
        """Validation says free; the database says no. Simulates the race."""
        api.force_authenticate(world['customer'])
        StudioBooking.objects.create(customer=world['other'], room=world['room'],
                                     engineer=world['eng_2'], service=world['svc'],
                                     start_at=next_monday_at(10), end_at=next_monday_at(14),
                                     total_pesewas=1, status=BookingStatus.CONFIRMED)
        with mock.patch('apps.studio.serializers.is_free', return_value=(True, '')):
            res = api.post(LIST(), payload(world), format='json')
        assert res.status_code == 409
        assert res.data['code'] == 'slot_taken'
        assert 'room' in res.data['detail']
        assert StudioBooking.objects.filter(customer=world['customer']).count() == 0


class TestListAndRetrieve:
    def test_customer_sees_only_their_own(self, api, world):
        api.force_authenticate(world['customer'])
        api.post(LIST(), payload(world), format='json')
        api.force_authenticate(world['other'])
        api.post(LIST(), payload(world, start_at=next_monday_at(15).isoformat(), hours=1),
                 format='json')

        api.force_authenticate(world['customer'])
        res = api.get(LIST())
        assert res.data['count'] == 1
        assert res.data['results'][0]['notes'] == 'Bring a cajon'

    def test_admin_sees_all(self, api, world):
        api.force_authenticate(world['customer'])
        api.post(LIST(), payload(world), format='json')
        api.force_authenticate(world['admin'])
        assert api.get(LIST()).data['count'] == 1

    def test_someone_elses_booking_is_404(self, api, world):
        api.force_authenticate(world['customer'])
        bid = api.post(LIST(), payload(world), format='json').data['id']
        api.force_authenticate(world['other'])
        assert api.get(reverse('studio:booking-detail', args=[bid])).status_code == 404
        api.force_authenticate(world['admin'])
        assert api.get(reverse('studio:booking-detail', args=[bid])).status_code == 200

    def test_booking_shows_as_booked_in_the_calendar(self, api, world):
        api.force_authenticate(world['customer'])
        api.post(LIST(), payload(world), format='json')
        day = next_monday_at(0).date()
        api.force_authenticate(None)
        res = api.get(reverse('studio:calendar'), {'room': world['room'].id, 'from': day, 'to': day})
        states = [s['state'] for s in res.data['slots']]
        assert states[1:5] == ['booked'] * 4 and states[0] == 'available'
