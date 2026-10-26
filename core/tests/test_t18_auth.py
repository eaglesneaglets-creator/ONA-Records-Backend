"""
Task 18 — sign-in lockout per account (API and Django admin) and the
verified-email gate on booking.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings as dj_settings
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.studio.models import Engineer, Room, Service
from core import lockout
from core.models import AuditLog

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def ama(django_user_model):
    return django_user_model.objects.create_user(email='ama@x.test', password='Right-Passw0rd!',
                                                 role='customer')


def login(email, password):
    return APIClient().post(reverse('accounts:login'), {'email': email, 'password': password},
                            format='json')


class TestApiLockout:
    def test_five_wrong_passwords_lock_the_account_even_with_the_right_one(self, ama):
        for _ in range(lockout.LOCKOUT_ATTEMPTS):
            assert login('ama@x.test', 'wrong').status_code == 400
        res = login('ama@x.test', 'Right-Passw0rd!')
        assert res.status_code == 429 and 'Too many failed sign-ins' in res.data['detail']
        log = AuditLog.objects.get(action='auth.locked_out')
        assert log.metadata['email'] == 'ama@x.test' and 'wrong' not in str(log.metadata)

    def test_case_and_spaces_do_not_dodge_the_count(self, ama):
        for email in ('AMA@x.test', ' ama@X.TEST ', 'Ama@x.test', 'ama@x.test', 'AMA@X.TEST'):
            login(email, 'wrong')
        assert login('ama@x.test', 'Right-Passw0rd!').status_code == 429

    def test_a_good_sign_in_resets_the_count(self, ama):
        for _ in range(lockout.LOCKOUT_ATTEMPTS - 1):
            login('ama@x.test', 'wrong')
        assert login('ama@x.test', 'Right-Passw0rd!').status_code == 200
        for _ in range(lockout.LOCKOUT_ATTEMPTS - 1):
            login('ama@x.test', 'wrong')
        assert login('ama@x.test', 'Right-Passw0rd!').status_code == 200

    def test_unknown_addresses_lock_the_same_way(self):
        """Same answer whether or not the account exists — no enumeration."""
        for _ in range(lockout.LOCKOUT_ATTEMPTS):
            assert login('nobody@x.test', 'x').status_code == 400
        assert login('nobody@x.test', 'x').status_code == 429

    def test_a_malformed_form_is_not_a_guess(self, ama):
        for _ in range(lockout.LOCKOUT_ATTEMPTS + 1):
            APIClient().post(reverse('accounts:login'), {'email': 'ama@x.test'}, format='json')
        assert login('ama@x.test', 'Right-Passw0rd!').status_code == 200

    def test_one_locked_account_does_not_lock_another(self, ama, django_user_model):
        django_user_model.objects.create_user(email='kofi@x.test', password='Right-Passw0rd!',
                                              role='customer')
        for _ in range(lockout.LOCKOUT_ATTEMPTS):
            login('ama@x.test', 'wrong')
        assert login('kofi@x.test', 'Right-Passw0rd!').status_code == 200

    def test_the_lock_expires(self, ama):
        for _ in range(lockout.LOCKOUT_ATTEMPTS):
            login('ama@x.test', 'wrong')
        lockout.clear('ama@x.test')  # what the cache TTL does after the window
        assert login('ama@x.test', 'Right-Passw0rd!').status_code == 200


class TestAdminLockout:
    def test_django_admin_locks_too(self, client, django_user_model):
        django_user_model.objects.create_superuser(email='boss@x.test', password='Right-Passw0rd!')
        url = '/' + dj_settings.ADMIN_URL + 'login/'
        for _ in range(lockout.LOCKOUT_ATTEMPTS):
            res = client.post(url, {'username': 'boss@x.test', 'password': 'wrong'})
            assert res.status_code == 200  # the form again, with an error
        res = client.post(url, {'username': 'boss@x.test', 'password': 'Right-Passw0rd!'})
        assert res.status_code == 200 and b'Too many failed sign-ins' in res.content
        lockout.clear('boss@x.test')
        res = client.post(url, {'username': 'boss@x.test', 'password': 'Right-Passw0rd!'})
        assert res.status_code == 302


class TestVerifiedEmailToBook:
    @pytest.fixture
    def world(self, ama):
        start = datetime.combine(date.today() + timedelta(days=5), datetime.min.time(),
                                 tzinfo=ZoneInfo('Africa/Accra')) + timedelta(hours=10)
        return {
            'data': {'room': str(Room.objects.create(name='A', hourly_rate_pesewas=100).pk),
                     'engineer': str(Engineer.objects.create(name='E').pk),
                     'service': str(Service.objects.create(name='S', base_price_pesewas=1,
                                                           duration_minutes=60).pk),
                     'start_at': start.isoformat(), 'hours': 1},
        }

    def book(self, user, data):
        c = APIClient()
        c.force_authenticate(user)
        return c.post(reverse('studio:booking-list'), data, format='json')

    def test_an_unverified_customer_cannot_book_when_the_gate_is_on(self, settings, ama, world):
        settings.BOOKING_REQUIRES_VERIFIED_EMAIL = True
        res = self.book(ama, world['data'])
        assert res.status_code == 403 and 'Confirm your email' in str(res.data)
        me = APIClient()
        me.force_authenticate(ama)
        assert me.get(reverse('accounts:me')).data['must_verify_email'] is True

        ama.email_verified_at = timezone.now()
        ama.save()
        assert self.book(ama, world['data']).status_code == 201
        assert me.get(reverse('accounts:me')).data['must_verify_email'] is False

    def test_the_gate_can_be_off_for_local_development(self, settings, ama, world):
        settings.BOOKING_REQUIRES_VERIFIED_EMAIL = False
        assert self.book(ama, world['data']).status_code == 201

    def test_reading_is_never_gated(self, settings, ama):
        settings.BOOKING_REQUIRES_VERIFIED_EMAIL = True
        c = APIClient()
        c.force_authenticate(ama)
        assert c.get(reverse('studio:booking-list')).status_code == 200

    def test_production_default_is_on(self):
        from config.settings import base
        assert base.BOOKING_REQUIRES_VERIFIED_EMAIL is True
