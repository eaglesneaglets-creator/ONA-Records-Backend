"""load_ona_catalogue: ONA's real catalogue in, placeholders off, safe to rerun."""

from io import StringIO

import pytest
from django.core.management import call_command

from apps.studio.management.commands.launch_check import collect
from apps.studio.models import Engineer, Package, PriceUnit, Room, Service

pytestmark = pytest.mark.django_db


def run(*args):
    out = StringIO()
    call_command('load_ona_catalogue', *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def placeholders():
    Room.objects.create(name='Studio A — Live Room', hourly_rate_pesewas=20000,
                        description='PLACEHOLDER.')
    Engineer.objects.create(name='Engineer One', bio='PLACEHOLDER')
    Service.objects.create(name='Recording session', base_price_pesewas=20000,
                           duration_minutes=60, description='PLACEHOLDER.')
    Package.objects.create(name='Half-day', hours=4, price_pesewas=70000)


def test_loads_onas_catalogue_and_switches_placeholders_off(placeholders):
    run()
    assert list(Room.objects.active().values_list('name', flat=True)) == ['Studio One']
    assert list(Engineer.objects.active().values_list('name', flat=True)) == ['Michael Kontor']
    assert Service.objects.active().count() == 10
    assert not Package.objects.active().exists()
    day = Service.objects.get(name='Full Day Studio Pass')
    assert (day.base_price_pesewas, day.price_unit) == (100000, PriceUnit.DAY)
    assert Service.objects.get(name='Mixing & Mastering').price_unit == PriceUnit.TRACK
    assert Room.objects.get(name='Studio One').hourly_rate_pesewas is None


def test_running_twice_changes_nothing(placeholders):
    run()
    second = run()
    assert '+ ' not in second and '~ ' not in second and '- ' not in second


def test_dry_run_saves_nothing(placeholders):
    run('--dry-run')
    assert not Room.objects.filter(name='Studio One').exists()
    assert Room.objects.get(name='Studio A — Live Room').is_active


def test_no_placeholder_blockers_remain(placeholders):
    run()
    assert not [m for lvl, m in collect() if lvl == 'BLOCKER' and 'Placeholder' in m]
