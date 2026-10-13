"""
Regression tests for the 2026-10-13 system review.

Each test pins a defect that was found by review and fixed; the docstring
names it. If one of these fails, the original bug is back.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.studio.availability import PAST, availability
from apps.studio.models import BookingStatus, CalendarBlock, Engineer, Room, Service, StudioBooking
from apps.studio.serializers import MAX_DAYS_AHEAD, MAX_OPEN_PENDING

pytestmark = pytest.mark.django_db

TZ = ZoneInfo('Africa/Accra')


def future_day(days=21):
    return date.today() + timedelta(days=days)


def at(day, hour):
    return datetime.combine(day, datetime.min.time(), tzinfo=TZ) + timedelta(hours=hour)


@pytest.fixture
def world(django_user_model):
    U = django_user_model.objects
    return {
        'customer': U.create_user(email='c@x.test', password='p-Ass-1', role='customer'),
        'admin': U.create_superuser(email='a@x.test', password='p-Ass-1'),
        'room': Room.objects.create(name='A', hourly_rate_pesewas=20000),
        'room_b': Room.objects.create(name='B', hourly_rate_pesewas=20000),
        'eng': Engineer.objects.create(name='One'),
        'eng_2': Engineer.objects.create(name='Two'),
        'svc': Service.objects.create(name='Rec', base_price_pesewas=1, duration_minutes=60),
    }


def booking(w, start, hours=1, **kw):
    kw.setdefault('room', w['room'])
    kw.setdefault('engineer', w['eng'])
    return StudioBooking.objects.create(
        customer=w['customer'], service=w['svc'], start_at=start,
        end_at=start + timedelta(hours=hours), total_pesewas=1, **kw)


class TestAdmin:
    """Saving a booking in Django admin 500'd: the read-only deleted_at was
    excluded from validation, and the exclusion constraint's condition needs it."""

    def admin_client(self, client, w):
        client.force_login(w['admin'])
        return client

    def form(self, b, **over):
        local = b.start_at.astimezone(TZ)
        end = b.end_at.astimezone(TZ)
        data = {
            'customer': str(b.customer_id), 'room': str(b.room_id), 'engineer': str(b.engineer_id),
            'service': str(b.service_id),
            'start_at_0': local.strftime('%Y-%m-%d'), 'start_at_1': local.strftime('%H:%M:%S'),
            'end_at_0': end.strftime('%Y-%m-%d'), 'end_at_1': end.strftime('%H:%M:%S'),
            'status': b.status, 'notes': b.notes,
        }
        data.update(over)
        return data

    def test_cancelling_a_booking_in_admin_saves(self, client, world):
        b = booking(world, at(future_day(), 10))
        url = reverse('admin:studio_studiobooking_change', args=[b.pk])
        res = self.admin_client(client, world).post(url, self.form(b, status='cancelled'))
        assert res.status_code == 302, res.content[:500]
        b.refresh_from_db()
        assert b.status == BookingStatus.CANCELLED

    def test_an_overlap_in_admin_is_a_form_error_not_a_500(self, client, world):
        day = future_day()
        booking(world, at(day, 10), hours=2)
        moved = booking(world, at(day, 14), room=world['room'], engineer=world['eng_2'])
        url = reverse('admin:studio_studiobooking_change', args=[moved.pk])
        res = self.admin_client(client, world).post(url, self.form(
            moved, start_at_1='11:00:00', end_at_1='12:00:00'))
        assert res.status_code == 200          # re-rendered with errors
        assert b'already booked' in res.content

    def test_add_and_delete_are_disabled(self, client, world):
        b = booking(world, at(future_day(), 10))
        c = self.admin_client(client, world)
        assert c.get(reverse('admin:studio_studiobooking_add')).status_code == 403
        assert c.get(reverse('admin:studio_studiobooking_delete', args=[b.pk])).status_code == 403

    def test_cancel_action_releases_the_slot(self, client, world):
        b = booking(world, at(future_day(), 10))
        c = self.admin_client(client, world)
        c.post(reverse('admin:studio_studiobooking_changelist'),
               {'action': 'cancel_bookings', '_selected_action': [str(b.pk)]})
        b.refresh_from_db()
        assert b.status == BookingStatus.CANCELLED
        booking(world, at(future_day(), 10))  # the slot is free again


class TestCatalogueNameAfterSoftDelete:
    """Reusing a soft-deleted room's name passed admin validation, then the
    unique index refused the insert with a 500."""

    def test_name_is_reusable_once_the_old_row_is_soft_deleted(self, world):
        world['room'].soft_delete()
        Room.objects.create(name='A', hourly_rate_pesewas=1)
        assert Room.all_objects.filter(name='A').count() == 2

    def test_name_is_still_unique_among_live_rows(self, world):
        with pytest.raises(IntegrityError), transaction.atomic():
            Room.objects.create(name='A', hourly_rate_pesewas=1)


class TestSlotSquatting:
    """Free, unexpiring pending bookings let one account hold every slot."""

    def post(self, w, start, hours=1):
        api = APIClient()
        api.force_authenticate(w['customer'])
        return api.post(reverse('studio:booking-list'), {
            'room': w['room'].id, 'engineer': w['eng'].id, 'service': w['svc'].id,
            'start_at': start.isoformat(), 'hours': hours}, format='json')

    def test_open_pending_bookings_are_capped(self, world):
        day = future_day()
        for i in range(MAX_OPEN_PENDING):
            assert self.post(world, at(day, 9 + i)).status_code == 201
        res = self.post(world, at(day, 15))
        assert res.status_code == 400
        assert 'waiting for confirmation' in str(res.data)

    def test_cancelled_bookings_do_not_count_toward_the_cap(self, world):
        day = future_day()
        for i in range(MAX_OPEN_PENDING):
            booking(world, at(day, 9 + i), status=BookingStatus.CANCELLED)
        assert self.post(world, at(day, 15)).status_code == 201

    def test_booking_too_far_ahead_is_refused(self, world):
        res = self.post(world, at(future_day(MAX_DAYS_AHEAD + 5), 10))
        assert res.status_code == 400 and 'days ahead' in str(res.data)


class TestPastSlots:
    """Earlier hours of today were shown as 'available' but could not be booked."""

    def test_slots_that_have_started_are_past(self, world):
        slots = availability(start=timezone.localdate() - timedelta(days=1),
                             end=timezone.localdate() - timedelta(days=1), room=world['room'])
        assert {s.state for s in slots} == {PAST}

    def test_a_past_slot_that_was_booked_still_says_booked(self, world):
        yesterday = timezone.localdate() - timedelta(days=1)
        booking(world, at(yesterday, 10), status=BookingStatus.COMPLETED)
        booking(world, at(yesterday, 12), status=BookingStatus.CONFIRMED)
        states = {s.start_at.astimezone(TZ).hour: s.state
                  for s in availability(start=yesterday, end=yesterday, room=world['room'])}
        assert states[12] == 'booked' and states[10] == PAST


class TestConflictEnvelope:
    """The 409 bypassed the error envelope; any IntegrityError was reported
    as 'slot taken'."""

    def test_409_uses_the_standard_envelope(self, world):
        from unittest import mock
        day = future_day()
        StudioBooking.objects.create(customer=world['customer'], room=world['room'],
                                     engineer=world['eng_2'], service=world['svc'],
                                     start_at=at(day, 10), end_at=at(day, 12), total_pesewas=1,
                                     status=BookingStatus.CONFIRMED)
        api = APIClient()
        api.force_authenticate(world['customer'])
        with mock.patch('apps.studio.serializers.is_free', return_value=(True, '')):
            res = api.post(reverse('studio:booking-list'), {
                'room': world['room'].id, 'engineer': world['eng'].id, 'service': world['svc'].id,
                'start_at': at(day, 10).isoformat(), 'hours': 1}, format='json')
        assert res.status_code == 409
        assert res.data['error']['code'] == 'slot_taken'


class TestSchemaGeneration:
    """BookingViewSet.get_queryset crashed for AnonymousUser during schema
    generation (seen as a warning in `check --deploy`)."""

    def test_schema_builds(self, client):
        res = client.get('/api/v1/schema/')
        assert res.status_code == 200
        assert b'/api/v1/studio/bookings/' in res.content


class TestCalendarBlockAdmin:
    def test_changelist_has_no_n_plus_one(self, client, world, django_assert_max_num_queries):
        for i in range(10):
            CalendarBlock.objects.create(kind='maintenance', room=world['room'],
                                         start_at=at(future_day(), 9 + i),
                                         end_at=at(future_day(), 10 + i))
        client.force_login(world['admin'])
        with django_assert_max_num_queries(12):
            assert client.get(reverse('admin:studio_calendarblock_changelist')).status_code == 200
