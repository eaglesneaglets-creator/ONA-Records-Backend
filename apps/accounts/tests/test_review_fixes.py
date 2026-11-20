"""
Regression tests for the 2026-10-13 security review of accounts and core.
"""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import serializers as drf_serializers
from rest_framework.test import APIClient

from apps.accounts.serializers import RegisterSerializer
from apps.accounts.tokens import email_verification_token
from core.models import client_ip

User = get_user_model()
pytestmark = pytest.mark.django_db

PASSWORD = 'a-long-enough-password'


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def user():
    return User.objects.create_user(email='kofi@example.com', password=PASSWORD, role='customer')


def uid(u):
    return urlsafe_base64_encode(force_bytes(u.pk))


class TestTokensAreSeparate:
    """The verification link doubled as a password-reset link, and logging in
    invalidated it."""

    def test_a_verification_token_cannot_reset_a_password(self, api, user):
        res = api.post(reverse('accounts:password-reset-confirm'), {
            'uid': uid(user), 'token': email_verification_token.make_token(user),
            'password': 'a-brand-new-password', 'password_confirm': 'a-brand-new-password',
        }, format='json')
        assert res.status_code == 400
        user.refresh_from_db()
        assert user.check_password(PASSWORD)

    def test_a_reset_token_cannot_verify_an_email(self, api, user):
        res = api.post(reverse('accounts:verify-email'), {
            'uid': uid(user), 'token': default_token_generator.make_token(user)}, format='json')
        assert res.status_code == 400

    def test_logging_in_does_not_invalidate_the_verification_link(self, api, user):
        token = email_verification_token.make_token(user)
        assert api.post(reverse('accounts:login'),
                        {'email': user.email, 'password': PASSWORD}, format='json').status_code == 200
        res = api.post(reverse('accounts:verify-email'), {'uid': uid(user), 'token': token},
                       format='json')
        assert res.status_code == 200

    def test_the_verification_link_is_single_use(self, api, user):
        token = email_verification_token.make_token(user)
        payload = {'uid': uid(user), 'token': token}
        assert api.post(reverse('accounts:verify-email'), payload, format='json').status_code == 200
        user.refresh_from_db()
        assert not email_verification_token.check_token(user, token)


class TestPasswordChangeEndsSessions:
    """A password reset left an attacker's refresh token working."""

    def test_old_refresh_token_is_refused_after_the_password_changes(self, api, user):
        tokens = api.post(reverse('accounts:login'), {'email': user.email, 'password': PASSWORD},
                          format='json').data['tokens']
        user.set_password('a-brand-new-password')
        user.save()
        res = api.post(reverse('accounts:token-refresh'), {'refresh': tokens['refresh']},
                       format='json')
        assert res.status_code == 401


class TestRegistrationRace:
    """Two simultaneous submits both passed the duplicate check; the second
    hit the unique index and 500'd."""

    def test_unique_violation_becomes_a_validation_error(self, user):
        ser = RegisterSerializer()
        with pytest.raises(drf_serializers.ValidationError) as exc:
            ser.create({'email': user.email, 'password': PASSWORD, 'role': 'customer',
                        'first_name': 'A', 'last_name': 'B'})
        assert 'email' in exc.value.detail


class TestClientIp:
    """The first X-Forwarded-For entry is client-controlled: it bypassed
    throttles and an invalid value silently dropped the audit row."""

    rf = RequestFactory()

    @override_settings(REST_FRAMEWORK={'NUM_PROXIES': 1})
    def test_takes_the_hop_the_proxy_added_not_the_one_the_client_sent(self):
        req = self.rf.get('/', HTTP_X_FORWARDED_FOR='6.6.6.6, 41.66.0.7', REMOTE_ADDR='10.0.0.1')
        assert client_ip(req) == '41.66.0.7'

    @override_settings(REST_FRAMEWORK={'NUM_PROXIES': 1})
    def test_garbage_is_dropped_not_saved(self):
        req = self.rf.get('/', HTTP_X_FORWARDED_FOR='not-an-ip', REMOTE_ADDR='10.0.0.1')
        assert client_ip(req) is None

    @override_settings(REST_FRAMEWORK={})
    def test_without_a_proxy_the_header_is_ignored(self):
        req = self.rf.get('/', HTTP_X_FORWARDED_FOR='6.6.6.6', REMOTE_ADDR='127.0.0.1')
        assert client_ip(req) == '127.0.0.1'
