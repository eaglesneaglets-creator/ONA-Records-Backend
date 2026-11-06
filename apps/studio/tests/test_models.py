"""
Catalogue model tests.

The thing worth proving here is the visibility rule: `objects.active()` is
what every public endpoint will use, so an inactive or soft-deleted room
leaking through it means a customer books something that does not exist.
"""

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError

from apps.studio.models import Engineer, Room, Service

pytestmark = pytest.mark.django_db


def make_room(name='Live Room', **kw):
    kw.setdefault('hourly_rate_pesewas', 20000)
    return Room.objects.create(name=name, **kw)


class TestVisibility:
    def test_active_excludes_switched_off_rows(self):
        make_room('On')
        make_room('Off', is_active=False)
        assert list(Room.objects.active().values_list('name', flat=True)) == ['On']

    def test_active_excludes_soft_deleted_rows(self):
        gone = make_room('Gone')
        make_room('Here')
        gone.soft_delete()
        assert list(Room.objects.active().values_list('name', flat=True)) == ['Here']
        # Default manager hides it too; only all_objects sees it.
        assert Room.objects.count() == 1
        assert Room.all_objects.count() == 2

    def test_same_rule_on_every_catalogue_model(self):
        Engineer.objects.create(name='E', is_active=False)
        Service.objects.create(name='S', base_price_pesewas=100, duration_minutes=60, is_active=False)
        assert not Engineer.objects.active().exists()
        assert not Service.objects.active().exists()

    def test_ordering_is_sort_order_then_name(self):
        make_room('B', sort_order=10)
        make_room('A', sort_order=10)
        make_room('Z', sort_order=1)
        assert list(Room.objects.values_list('name', flat=True)) == ['Z', 'A', 'B']


class TestMoney:
    def test_rate_is_integer_pesewas_and_formats_as_ghs(self):
        room = make_room()
        assert room.hourly_rate_pesewas == 20000
        assert room.hourly_rate_display == 'GHS 200.00'

    def test_service_price_formats_as_ghs(self):
        svc = Service.objects.create(name='Mix', base_price_pesewas=30050, duration_minutes=60)
        assert svc.base_price_display == 'GHS 300.50'

    def test_zero_price_is_rejected(self):
        # A free room is almost certainly a typo. Reject at validation so
        # admin shows the error instead of a customer seeing GHS 0.00.
        room = Room(name='Free?', hourly_rate_pesewas=0)
        with pytest.raises(ValidationError):
            room.full_clean()


class TestConstraints:
    def test_name_is_unique(self):
        make_room('Live Room')
        with pytest.raises(IntegrityError):
            make_room('Live Room')

    def test_service_duration_has_a_floor(self):
        svc = Service(name='Blink', base_price_pesewas=100, duration_minutes=5)
        with pytest.raises(ValidationError):
            svc.full_clean()

    def test_str_is_the_name(self):
        assert str(make_room('Mix Suite')) == 'Mix Suite'


class TestSeeder:
    def test_seeds_every_table_and_is_idempotent(self):
        call_command('seed_studio')
        first = (Room.objects.count(), Engineer.objects.count(), Service.objects.count())
        assert all(n > 0 for n in first)

        call_command('seed_studio')
        second = (Room.objects.count(), Engineer.objects.count(), Service.objects.count())
        assert first == second

    def test_seeded_rows_are_active_and_visible(self):
        call_command('seed_studio')
        assert Room.objects.active().count() == Room.objects.count()

    def test_rerun_corrects_an_edited_row_but_leaves_staff_rows_alone(self):
        call_command('seed_studio')
        staff = make_room('ONA Added This', hourly_rate_pesewas=999)
        seeded = Room.objects.exclude(pk=staff.pk).first()
        seeded.hourly_rate_pesewas = 1
        seeded.save()

        call_command('seed_studio')
        seeded.refresh_from_db()
        staff.refresh_from_db()
        assert seeded.hourly_rate_pesewas != 1
        assert staff.hourly_rate_pesewas == 999

    def test_refuses_outside_local_unless_forced_and_never_on_production(self, settings):
        from django.core.management.base import CommandError

        settings.ENVIRONMENT_NAME = 'staging'
        with pytest.raises(CommandError):
            call_command('seed_studio')
        call_command('seed_studio', force=True)
        assert Room.objects.exists()

        settings.ENVIRONMENT_NAME = 'production'
        with pytest.raises(CommandError):
            call_command('seed_studio', force=True)
