"""/api/v1/admin/staff/ — adding, deactivating and re-inviting ONA staff."""

from unittest import mock

import pytest
from rest_framework.test import APIClient

from apps.notifications.models import Kind
from core.models import AuditLog

pytestmark = pytest.mark.django_db
URL = '/api/v1/admin/staff/'


@pytest.fixture
def boss(django_user_model):
    return django_user_model.objects.create_superuser(email='boss@ona.test', password='x')


def errors(r) -> dict:
    return r.json()['error']['detail']


def as_user(user) -> APIClient:
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.fixture
def sent():
    with mock.patch('apps.accounts.team.notify_account') as notify:
        yield notify


def test_add_a_staff_member_sends_an_invite(boss, sent, django_user_model):
    r = as_user(boss).post(URL, {'email': ' Kofi@ONA.test ', 'first_name': 'Kofi'}, format='json')
    assert r.status_code == 201, r.content
    body = r.json()
    assert body['email'] == 'kofi@ona.test' and body['invite_pending'] is True
    member = django_user_model.objects.get(email='kofi@ona.test')
    assert member.role == 'admin' and not member.has_usable_password()
    sent.assert_called_once_with(member, Kind.STAFF_INVITED)
    assert AuditLog.objects.filter(action=AuditLog.Action.STAFF_INVITED, actor=boss).exists()


def test_a_customer_address_is_refused_not_converted(boss, sent, django_user_model):
    django_user_model.objects.create_user(email='ama@x.test', password='x', role='customer')
    r = as_user(boss).post(URL, {'email': 'ama@x.test', 'first_name': 'Ama'}, format='json')
    assert r.status_code == 400 and 'email' in errors(r)
    assert django_user_model.objects.get(email='ama@x.test').role == 'customer'
    sent.assert_not_called()


def test_cannot_deactivate_yourself_or_the_last_admin(boss, django_user_model):
    r = as_user(boss).patch(f'{URL}{boss.pk}/', {'is_active': False}, format='json')
    assert r.status_code == 400 and 'is_active' in errors(r)

    other = django_user_model.objects.create_user(email='k@ona.test', password='x', role='admin')
    boss.is_active = False   # pretend only `other` is left active
    boss.save()
    r = as_user(other).patch(f'{URL}{other.pk}/', {'is_active': False}, format='json')
    assert r.status_code == 400


def test_deactivating_someone_locks_them_out_at_once(boss, django_user_model):
    kofi = django_user_model.objects.create_user(email='k@ona.test', password='Pw-2026-kofi!',
                                                 role='admin')
    login = APIClient().post('/api/v1/auth/login/', {'email': 'k@ona.test',
                                                     'password': 'Pw-2026-kofi!'}, format='json')
    token = login.json()['tokens']['access']

    r = as_user(boss).patch(f'{URL}{kofi.pk}/', {'is_active': False}, format='json')
    assert r.status_code == 200 and r.json()['is_active'] is False
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    assert c.get(URL).status_code == 401
    assert AuditLog.objects.filter(action=AuditLog.Action.USER_SUSPENDED, actor=boss).exists()


def test_resend_invite_only_while_pending(boss, sent, django_user_model):
    pending = django_user_model.objects.create_user(email='p@ona.test', password=None, role='admin')
    done = django_user_model.objects.create_user(email='d@ona.test', password='x', role='admin')
    c = as_user(boss)
    assert c.post(f'{URL}{pending.pk}/resend-invite/').status_code == 200
    assert c.post(f'{URL}{done.pk}/resend-invite/').status_code == 409
    sent.assert_called_once_with(pending, Kind.STAFF_INVITED)


def test_list_shows_staff_only_and_marks_you(boss, django_user_model):
    django_user_model.objects.create_user(email='c@x.test', password='x', role='customer')
    rows = as_user(boss).get(URL).json()
    assert [r['email'] for r in rows] == ['boss@ona.test'] and rows[0]['is_you'] is True


def test_customers_are_refused(django_user_model):
    c = as_user(django_user_model.objects.create_user(email='c@x.test', password='x'))
    assert c.get(URL).status_code == 403
    assert c.post(URL, {'email': 'x@x.test', 'first_name': 'X'}, format='json').status_code == 403
