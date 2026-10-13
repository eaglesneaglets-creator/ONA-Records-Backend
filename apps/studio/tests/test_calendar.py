"""
Availability: the four SRS states, and the public calendar endpoint.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.studio.availability import (
    AVAILABLE, BOOKED, CLOSE_HOUR, HOLIDAY, MAINTENANCE, OPEN_HOUR, availability, is_free,
)
from apps.studio.models import BlockKind, BookingStatus, CalendarBlock, Engineer, Room, Service, StudioBooking

pytestmark = pytest.mark.django_db

TZ = ZoneInfo('Africa/Accra')
# Always in the future: past slots are reported as 'past', so a fixed date
# would start failing the day it went by.
DAY = date.today() + timedelta(days=21)


def at(hour, day=DAY):
    return datetime.combine(day, datetime.min.time(), tzinfo=TZ) + timedelta(hours=hour)


@pytest.fixture
def world(django_user_model):
    return {
        'customer': django_user_model.objects.create_user(email='c@x.test', password='p-Ass-1'),
        'room': Room.objects.create(name='A', hourly_rate_pesewas=20000),
        'room_b': Room.objects.create(name='B', hourly_rate_pesewas=12000),
        'eng': Engineer.objects.create(name='One'),
        'eng_2': Engineer.objects.create(name='Two'),
        'svc': Service.objects.create(name='Rec', base_price_pesewas=20000, duration_minutes=60),
    }


def states(slots):
    return {s.start_at.hour: s.state for s in slots}


class TestFourStates:
    def test_empty_day_is_all_available_within_opening_hours(self, world):
        slots = availability(start=DAY, end=DAY, room=world['room'])
        assert len(slots) == CLOSE_HOUR - OPEN_HOUR
        assert set(states(slots).values()) == {AVAILABLE}
        assert slots[0].start_at.hour == OPEN_HOUR

    def test_booking_marks_booked(self, world):
        StudioBooking.objects.create(customer=world['customer'], room=world['room'],
                                     engineer=world['eng'], service=world['svc'],
                                     start_at=at(10), end_at=at(12), total_pesewas=1)
        st = states(availability(start=DAY, end=DAY, room=world['room']))
        assert st[10] == BOOKED and st[11] == BOOKED
        assert st[9] == AVAILABLE and st[12] == AVAILABLE

    def test_cancelled_booking_does_not_mark_booked(self, world):
        StudioBooking.objects.create(customer=world['customer'], room=world['room'],
                                     engineer=world['eng'], service=world['svc'],
                                     start_at=at(10), end_at=at(12), total_pesewas=1,
                                     status=BookingStatus.CANCELLED)
        assert states(availability(start=DAY, end=DAY, room=world['room']))[10] == AVAILABLE

    def test_room_maintenance_marks_maintenance(self, world):
        CalendarBlock.objects.create(kind=BlockKind.MAINTENANCE, room=world['room'],
                                     start_at=at(14), end_at=at(16))
        st = states(availability(start=DAY, end=DAY, room=world['room']))
        assert st[14] == MAINTENANCE and st[15] == MAINTENANCE and st[16] == AVAILABLE
        # Other room unaffected.
        assert states(availability(start=DAY, end=DAY, room=world['room_b']))[14] == AVAILABLE

    def test_studio_wide_holiday_marks_holiday_for_everyone(self, world):
        CalendarBlock.objects.create(kind=BlockKind.HOLIDAY, start_at=at(0), end_at=at(24))
        for scope in ({'room': world['room']}, {'engineer': world['eng']}, {}):
            assert set(states(availability(start=DAY, end=DAY, **scope)).values()) == {HOLIDAY}

    def test_holiday_beats_maintenance_beats_booked(self, world):
        StudioBooking.objects.create(customer=world['customer'], room=world['room'],
                                     engineer=world['eng'], service=world['svc'],
                                     start_at=at(10), end_at=at(11), total_pesewas=1)
        CalendarBlock.objects.create(kind=BlockKind.MAINTENANCE, room=world['room'],
                                     start_at=at(10), end_at=at(11))
        assert states(availability(start=DAY, end=DAY, room=world['room']))[10] == MAINTENANCE
        CalendarBlock.objects.create(kind=BlockKind.HOLIDAY, start_at=at(10), end_at=at(11))
        assert states(availability(start=DAY, end=DAY, room=world['room']))[10] == HOLIDAY

    def test_engineer_booked_elsewhere_blocks_that_engineer_only(self, world):
        StudioBooking.objects.create(customer=world['customer'], room=world['room_b'],
                                     engineer=world['eng'], service=world['svc'],
                                     start_at=at(10), end_at=at(11), total_pesewas=1)
        assert states(availability(start=DAY, end=DAY, engineer=world['eng']))[10] == BOOKED
        assert states(availability(start=DAY, end=DAY, engineer=world['eng_2']))[10] == AVAILABLE
        # Room A alone is free; room A + engineer One together is not.
        assert states(availability(start=DAY, end=DAY, room=world['room']))[10] == AVAILABLE
        assert states(availability(start=DAY, end=DAY, room=world['room'],
                                   engineer=world['eng']))[10] == BOOKED

    def test_multi_day_range(self, world):
        slots = availability(start=DAY, end=DAY + timedelta(days=2), room=world['room'])
        assert len(slots) == 3 * (CLOSE_HOUR - OPEN_HOUR)

    def test_range_is_capped(self, world):
        with pytest.raises(ValueError):
            availability(start=DAY, end=DAY + timedelta(days=40), room=world['room'])


class TestIsFree:
    def test_free_slot(self, world):
        ok, _ = is_free(room=world['room'], engineer=world['eng'], start_at=at(10), end_at=at(12))
        assert ok

    def test_clash_gives_a_readable_reason(self, world):
        StudioBooking.objects.create(customer=world['customer'], room=world['room'],
                                     engineer=world['eng_2'], service=world['svc'],
                                     start_at=at(10), end_at=at(12), total_pesewas=1)
        ok, why = is_free(room=world['room'], engineer=world['eng'], start_at=at(11), end_at=at(13))
        assert not ok and 'room' in why

    def test_block_gives_a_readable_reason(self, world):
        CalendarBlock.objects.create(kind=BlockKind.MAINTENANCE, engineer=world['eng'],
                                     start_at=at(10), end_at=at(12))
        ok, why = is_free(room=world['room'], engineer=world['eng'], start_at=at(11), end_at=at(13))
        assert not ok and 'maintenance' in why


class TestCalendarEndpoint:
    @pytest.fixture
    def api(self):
        return APIClient()

    def test_public_and_defaults_to_two_weeks(self, api, world):
        res = api.get(reverse('studio:calendar'), {'room': world['room'].id})
        assert res.status_code == 200
        from django.utils import timezone
        assert res.data['from'] == timezone.localdate()
        assert (res.data['to'] - res.data['from']).days == 13
        assert len(res.data['slots']) == 14 * (CLOSE_HOUR - OPEN_HOUR)
        # Today's earlier hours may be 'past'; nothing else is.
        assert {s['state'] for s in res.data['slots']} <= {'available', 'past'}
        assert {'start_at', 'end_at', 'state'} == set(res.data['slots'][0])

    def test_reports_all_four_states_and_no_identity(self, api, world):
        StudioBooking.objects.create(customer=world['customer'], room=world['room'],
                                     engineer=world['eng'], service=world['svc'],
                                     start_at=at(10), end_at=at(11), total_pesewas=1)
        CalendarBlock.objects.create(kind=BlockKind.MAINTENANCE, room=world['room'],
                                     start_at=at(12), end_at=at(13))
        CalendarBlock.objects.create(kind=BlockKind.HOLIDAY, start_at=at(14), end_at=at(15))
        res = api.get(reverse('studio:calendar'),
                      {'room': world['room'].id, 'from': DAY, 'to': DAY})
        by_hour = {datetime.fromisoformat(s['start_at']).astimezone(TZ).hour: s['state']
                   for s in res.data['slots']}
        assert by_hour[9] == AVAILABLE and by_hour[10] == BOOKED
        assert by_hour[12] == MAINTENANCE and by_hour[14] == HOLIDAY
        assert 'customer' not in str(res.content)

    def test_rejects_bad_ranges_and_unknown_room(self, api, world):
        assert api.get(reverse('studio:calendar'),
                       {'from': DAY, 'to': DAY - timedelta(days=1)}).status_code == 400
        assert api.get(reverse('studio:calendar'),
                       {'from': DAY, 'to': DAY + timedelta(days=60)}).status_code == 400
        assert api.get(reverse('studio:calendar'),
                       {'room': '00000000-0000-0000-0000-000000000000'}).status_code == 400

    def test_inactive_room_is_not_a_valid_scope(self, api, world):
        world['room'].is_active = False
        world['room'].save()
        assert api.get(reverse('studio:calendar'), {'room': world['room'].id}).status_code == 400
