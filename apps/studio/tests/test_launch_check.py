"""Task 19 — `manage.py launch_check` finds what would embarrass a launch."""

import pytest
from django.core.management import CommandError, call_command

from apps.content.models import Faq
from apps.studio.management.commands.launch_check import collect
from apps.studio.models import Engineer, Room, Service

pytestmark = pytest.mark.django_db


@pytest.fixture
def ready(settings):
    settings.DEBUG = False
    settings.ADMIN_URL = 'staff-7f3a/'
    settings.EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    settings.BOOKING_REQUIRES_VERIFIED_EMAIL = True
    settings.CELERY_BROKER_URL = 'redis://x'
    settings.HUBTEL_API_KEY = settings.HUBTEL_API_SECRET = settings.HUBTEL_MERCHANT_ID = 'set'
    settings.STUDIO_COMMISSION_PERCENT = 10
    Faq.objects.all().delete()
    Room.objects.create(name='Live Room', description='Real tracking room.', hourly_rate_pesewas=20000)
    Engineer.objects.create(name='Kojo', bio='Mix engineer.')
    Service.objects.create(name='Recording', base_price_pesewas=1, duration_minutes=60)


def test_a_ready_environment_passes(ready, capsys):
    call_command('launch_check')
    assert 'Ready: no blockers.' in capsys.readouterr().out


def test_placeholder_catalogue_text_blocks_launch(ready):
    Room.objects.create(name='Booth', description='Treated booth. PLACEHOLDER.', hourly_rate_pesewas=1)
    Engineer.objects.create(name='Temp', bio='PLACEHOLDER — replace with a real ONA engineer.')
    msgs = [m for lvl, m in collect() if lvl == 'BLOCKER']
    assert any('room "Booth"' in m for m in msgs) and any('engineer "Temp"' in m for m in msgs)
    with pytest.raises(CommandError, match='blocker'):
        call_command('launch_check')


def test_a_switched_off_placeholder_is_fine(ready):
    Room.objects.create(name='Old', description='PLACEHOLDER', hourly_rate_pesewas=1, is_active=False)
    assert not [m for lvl, m in collect() if lvl == 'BLOCKER']


def test_unsafe_settings_are_blockers(ready, settings):
    settings.DEBUG = True
    settings.ADMIN_URL = 'admin/'
    settings.EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
    settings.BOOKING_REQUIRES_VERIFIED_EMAIL = False
    blockers = ' '.join(m for lvl, m in collect() if lvl == 'BLOCKER')
    for needle in ('DEBUG', 'ADMIN_URL', 'Email is not sent', 'BOOKING_REQUIRES_VERIFIED_EMAIL'):
        assert needle in blockers


def test_nothing_to_book_is_a_blocker(settings):
    settings.DEBUG = False
    msgs = ' '.join(m for lvl, m in collect() if lvl == 'BLOCKER')
    assert 'No rooms' in msgs and 'No engineers' in msgs


def test_missing_payments_and_commission_are_warnings(ready, settings):
    settings.HUBTEL_API_KEY = ''
    settings.STUDIO_COMMISSION_PERCENT = 0
    settings.CELERY_BROKER_URL = ''
    warns = ' '.join(m for lvl, m in collect() if lvl == 'WARN')
    assert 'Hubtel' in warns and 'COMMISSION' in warns and 'Celery' in warns


def test_placeholder_faq_blocks(ready):
    Faq.objects.create(question='Lorem ipsum?', answer='Dolor.')
    assert any('FAQ' in m for lvl, m in collect() if lvl == 'BLOCKER')
