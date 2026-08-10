"""
JWT claim tests.

The frontend routes on `role` in the access token. If these claims go missing
the user lands in the wrong portal, or the app has to call /me on every page
load and show a flash of the wrong shell first.
"""

import base64
import json

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

User = get_user_model()
pytestmark = pytest.mark.django_db


def claims(token: str) -> dict:
    payload = token.split('.')[1]
    payload += '=' * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def admin_user():
    return User.objects.create_user(
        email='ama@onarecords.com', password='a-long-enough-password', role='admin',
    )


class TestAccessTokenClaims:
    def test_carries_role(self, api):
        r = api.post(reverse('accounts:register'), {
            'email': 'pro@example.com', 'password': 'a-long-enough-password',
            'password_confirm': 'a-long-enough-password', 'role': 'professional',
        }, format='json')
        assert claims(r.data['tokens']['access'])['role'] == 'professional'

    def test_carries_verification_and_approval(self, api):
        r = api.post(reverse('accounts:register'), {
            'email': 'pro2@example.com', 'password': 'a-long-enough-password',
            'password_confirm': 'a-long-enough-password', 'role': 'professional',
        }, format='json')
        c = claims(r.data['tokens']['access'])
        # A brand-new professional is neither verified nor approved.
        assert c['is_email_verified'] is False
        assert c['is_approved'] is False

    def test_customer_is_approved_by_definition(self, api):
        """Only professionals go through approval."""
        r = api.post(reverse('accounts:register'), {
            'email': 'cust@example.com', 'password': 'a-long-enough-password',
            'password_confirm': 'a-long-enough-password', 'role': 'customer',
        }, format='json')
        assert r.status_code == 201, r.data
        assert claims(r.data['tokens']['access'])['is_approved'] is True

    def test_login_carries_the_same_claims(self, api):
        User.objects.create_user(
            email='login@example.com', password='a-long-enough-password', role='customer',
        )
        r = api.post(reverse('accounts:login'), {
            'email': 'login@example.com', 'password': 'a-long-enough-password',
        }, format='json')
        assert claims(r.data['tokens']['access'])['role'] == 'customer'


class TestRefreshKeepsClaims:
    def test_refresh_preserves_role(self, api):
        """
        simplejwt's stock refresh drops custom claims. Without the override
        the frontend loses `role` 30 minutes into a session.
        """
        reg = api.post(reverse('accounts:register'), {
            'email': 'refresh@example.com', 'password': 'a-long-enough-password',
            'password_confirm': 'a-long-enough-password', 'role': 'professional',
        }, format='json')

        r = api.post(reverse('accounts:token-refresh'),
                     {'refresh': reg.data['tokens']['refresh']}, format='json')
        assert r.status_code == 200
        assert claims(r.data['access'])['role'] == 'professional'

    def test_refresh_reflects_approval_granted_mid_session(self, api, admin_user):
        """
        Claims are re-read from the database on refresh, so a professional
        approved during a session picks it up without signing in again.
        """
        reg = api.post(reverse('accounts:register'), {
            'email': 'approved@example.com', 'password': 'a-long-enough-password',
            'password_confirm': 'a-long-enough-password', 'role': 'professional',
        }, format='json')
        assert claims(reg.data['tokens']['access'])['is_approved'] is False

        user = User.objects.get(email='approved@example.com')
        user.approve(by=admin_user)

        r = api.post(reverse('accounts:token-refresh'),
                     {'refresh': reg.data['tokens']['refresh']}, format='json')
        assert claims(r.data['access'])['is_approved'] is True


class TestApproval:
    def test_professional_starts_unapproved(self):
        u = User.objects.create_user(
            email='p@example.com', password='a-long-enough-password', role='professional',
        )
        assert u.is_approved is False

    def test_customer_needs_no_approval(self):
        u = User.objects.create_user(
            email='c@example.com', password='a-long-enough-password', role='customer',
        )
        assert u.is_approved is True

    def test_approve_records_who_and_when(self, admin_user):
        u = User.objects.create_user(
            email='p2@example.com', password='a-long-enough-password', role='professional',
        )
        u.approve(by=admin_user)
        u.refresh_from_db()
        assert u.is_approved is True
        assert u.approved_by == admin_user
        assert u.approved_at is not None

    def test_approve_is_idempotent(self, admin_user):
        """Re-approving must not overwrite the original timestamp."""
        u = User.objects.create_user(
            email='p3@example.com', password='a-long-enough-password', role='professional',
        )
        u.approve(by=admin_user)
        first = u.approved_at
        u.approve(by=admin_user)
        u.refresh_from_db()
        assert u.approved_at == first
