"""
THE Phase 1 proof: two overlapping slot-holding bookings cannot both exist.

Every test here bypasses application validation and writes straight to the
database, because that is the only way to prove the guarantee is in the
database. A serializer that says no is nice; a constraint that says no is
the actual promise ("the booking must never lie" — PRODUCT.md).
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.db import IntegrityError, transaction

from apps.studio.models import BookingStatus, Engineer, Room, Service, StudioBooking

pytestmark = pytest.mark.django_db

TZ = ZoneInfo('Africa/Accra')
T0 = datetime(2026, 11, 2, 10, 0, tzinfo=TZ)


@pytest.fixture
def world(django_user_model):
    return {
        'customer': django_user_model.objects.create_user(email='c@x.test', password='p-Ass-1'),
        'other': django_user_model.objects.create_user(email='o@x.test', password='p-Ass-1'),
        'room_a': Room.objects.create(name='A', hourly_rate_pesewas=20000),
        'room_b': Room.objects.create(name='B', hourly_rate_pesewas=12000),
        'eng_1': Engineer.objects.create(name='One'),
        'eng_2': Engineer.objects.create(name='Two'),
        'svc': Service.objects.create(name='Rec', base_price_pesewas=20000, duration_minutes=60),
    }


def book(w, *, room='room_a', eng='eng_1', who='customer', start=T0, hours=2,
         status=BookingStatus.CONFIRMED):
    return StudioBooking.objects.create(
        customer=w[who], room=w[room], engineer=w[eng], service=w['svc'],
        start_at=start, end_at=start + timedelta(hours=hours), status=status,
        total_pesewas=w[room].hourly_rate_pesewas * hours,
    )


def raw_insert_raises(fn):
    with pytest.raises(IntegrityError) as exc, transaction.atomic():
        fn()
    return str(exc.value)


class TestRoomOverlap:
    def test_same_room_same_time_is_rejected_by_the_database(self, world):
        book(world)
        msg = raw_insert_raises(lambda: book(world, who='other', eng='eng_2'))
        assert 'studio_booking_no_room_overlap' in msg

    def test_partial_overlap_is_rejected(self, world):
        book(world, start=T0, hours=2)                       # 10:00–12:00
        raw_insert_raises(lambda: book(world, who='other', eng='eng_2',
                                       start=T0 + timedelta(hours=1)))  # 11:00–13:00

    def test_enclosing_booking_is_rejected(self, world):
        book(world, start=T0 + timedelta(hours=1), hours=1)  # 11:00–12:00
        raw_insert_raises(lambda: book(world, who='other', eng='eng_2',
                                       start=T0, hours=4))   # 10:00–14:00

    def test_adjacent_bookings_are_allowed(self, world):
        # Half-open ranges: ending at 12:00 and starting at 12:00 do not touch.
        book(world, start=T0, hours=2)
        book(world, who='other', eng='eng_2', start=T0 + timedelta(hours=2), hours=1)
        assert StudioBooking.objects.count() == 2

    def test_different_rooms_same_time_are_allowed(self, world):
        book(world, room='room_a')
        book(world, room='room_b', who='other', eng='eng_2')
        assert StudioBooking.objects.count() == 2


class TestEngineerOverlap:
    def test_same_engineer_different_rooms_is_rejected(self, world):
        book(world, room='room_a', eng='eng_1')
        msg = raw_insert_raises(lambda: book(world, room='room_b', eng='eng_1', who='other'))
        assert 'studio_booking_no_engineer_overlap' in msg

    def test_different_engineers_same_room_still_rejected_by_room_rule(self, world):
        book(world, eng='eng_1')
        raw_insert_raises(lambda: book(world, eng='eng_2', who='other'))


class TestWhichStatusesHoldASlot:
    def test_pending_holds_the_slot(self, world):
        # A customer mid-payment must not lose the hour to a faster clicker.
        book(world, status=BookingStatus.PENDING)
        raw_insert_raises(lambda: book(world, who='other', eng='eng_2'))

    @pytest.mark.parametrize('released', [BookingStatus.CANCELLED, BookingStatus.COMPLETED])
    def test_cancelled_and_completed_release_the_slot(self, world, released):
        book(world, status=released)
        book(world, who='other', eng='eng_2')
        assert StudioBooking.objects.count() == 2

    def test_cancelling_frees_the_slot_for_the_next_customer(self, world):
        first = book(world)
        first.status = BookingStatus.CANCELLED
        first.save()
        book(world, who='other', eng='eng_2')

    def test_soft_deleted_booking_releases_the_slot(self, world):
        first = book(world)
        first.soft_delete()
        book(world, who='other', eng='eng_2')


class TestReference:
    def test_reference_is_generated_unique_and_prefixed(self, world):
        a, b = book(world), book(world, room='room_b', eng='eng_2', who='other')
        assert a.reference != b.reference
        assert a.reference.startswith('ONA-') and len(a.reference) == 10

    def test_reference_collision_is_retried(self, world, monkeypatch):
        first = book(world)
        calls = iter([first.reference, 'ONA-FRESH1'])
        monkeypatch.setattr('apps.studio.models.generate_reference', lambda: next(calls))
        second = StudioBooking(customer=world['other'], room=world['room_b'],
                               engineer=world['eng_2'], service=world['svc'],
                               start_at=T0, end_at=T0 + timedelta(hours=1), total_pesewas=1,
                               reference=first.reference)
        second.save()
        assert second.reference == 'ONA-FRESH1'

    def test_money_is_frozen_on_the_booking(self, world):
        b = book(world, hours=4)
        assert b.total_pesewas == 80000
        assert b.total_display == 'GHS 800.00'
        world['room_a'].hourly_rate_pesewas = 1
        world['room_a'].save()
        b.refresh_from_db()
        assert b.total_pesewas == 80000
