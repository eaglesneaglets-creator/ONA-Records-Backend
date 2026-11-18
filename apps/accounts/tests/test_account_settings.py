"""Customer (and staff) account settings: edit your details, change your password."""

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from core.models import AuditLog

pytestmark = pytest.mark.django_db

PASSWORD = 'Old-pass-2026!'


@pytest.fixture
def me(django_user_model):
    return django_user_model.objects.create_user(
        email='ama@x.test', password=PASSWORD, role='customer', first_name='Ama')


def errors(r) -> dict:
    return r.json()['error']['detail']


def signed_in(user) -> tuple[APIClient, dict]:
    c = APIClient()
    tokens = c.post(reverse('accounts:login'), {'email': user.email, 'password': PASSWORD},
                    format='json').json()['tokens']
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    return c, tokens


class TestProfile:
    def test_edit_name_and_phone(self, me):
        c, _ = signed_in(me)
        r = c.patch(reverse('accounts:me'), {'first_name': '  Ama ', 'last_name': 'Mensah',
                                             'phone': '024 123 4567'}, format='json')
        assert r.status_code == 200, r.content
        me.refresh_from_db()
        assert (me.first_name, me.last_name, me.phone) == ('Ama', 'Mensah', '024 123 4567')

    @pytest.mark.parametrize('phone', ['12', 'call me', '+233 24 123 4567 999 999'])
    def test_a_bad_phone_is_refused_on_the_field(self, me, phone):
        c, _ = signed_in(me)
        r = c.patch(reverse('accounts:me'), {'phone': phone}, format='json')
        assert r.status_code == 400 and 'phone' in errors(r)

    def test_email_and_role_cannot_be_changed_here(self, me):
        c, _ = signed_in(me)
        c.patch(reverse('accounts:me'), {'email': 'new@x.test', 'role': 'admin'}, format='json')
        me.refresh_from_db()
        assert (me.email, me.role) == ('ama@x.test', 'customer')


class TestChangePassword:
    url = reverse('accounts:password-change')

    def test_wrong_current_password(self, me):
        c, _ = signed_in(me)
        r = c.post(self.url, {'current_password': 'nope', 'new_password': 'New-pass-2026!'},
                   format='json')
        assert r.status_code == 400 and 'current_password' in errors(r)

    def test_same_or_weak_new_password(self, me):
        c, _ = signed_in(me)
        same = c.post(self.url, {'current_password': PASSWORD, 'new_password': PASSWORD},
                      format='json')
        weak = c.post(self.url, {'current_password': PASSWORD, 'new_password': '123'},
                      format='json')
        assert 'new_password' in errors(same) and 'new_password' in errors(weak)

    def test_change_signs_out_other_sessions_and_keeps_this_one(self, me):
        c, _ = signed_in(me)
        other, other_tokens = signed_in(me)          # a second device
        r = c.post(self.url, {'current_password': PASSWORD, 'new_password': 'New-pass-2026!'},
                   format='json')
        assert r.status_code == 200, r.content
        body = r.json()['tokens']
        assert body['access'] and body['refresh']

        me.refresh_from_db()
        assert me.check_password('New-pass-2026!')
        # The other device's tokens die with the old password.
        assert other.get(reverse('accounts:me')).status_code == 401
        refreshed = APIClient().post(reverse('accounts:token-refresh'),
                                     {'refresh': other_tokens['refresh']}, format='json')
        assert refreshed.status_code == 401
        # This device carries on with the fresh pair.
        fresh = APIClient()
        fresh.credentials(HTTP_AUTHORIZATION=f"Bearer {body['access']}")
        assert fresh.get(reverse('accounts:me')).status_code == 200
        assert AuditLog.objects.filter(action=AuditLog.Action.PASSWORD_CHANGED, actor=me).exists()

    def test_requires_sign_in(self):
        r = APIClient().post(self.url, {'current_password': 'a', 'new_password': 'b'}, format='json')
        assert r.status_code == 401
