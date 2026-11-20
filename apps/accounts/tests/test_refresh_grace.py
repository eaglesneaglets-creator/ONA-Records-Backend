"""
D8 — a reload during the silent refresh must not sign the customer out.

The refresh already rotated on the server; the browser never stored the new
token and sends the old one again. Within the grace window that returns the
SAME successor. Outside it — or for a forged token, after logout, or after a
password change — it is refused exactly as before.
"""

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

pytestmark = pytest.mark.django_db
URL = '/api/v1/auth/refresh/'


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(email='g@x.test', password='p-Ass-1',
                                                 role='customer')


def refresh(raw):
    return APIClient().post(URL, {'refresh': raw}, format='json')


def test_a_replay_moments_later_gets_the_same_successor(user):
    old = str(RefreshToken.for_user(user))
    first = refresh(old)
    assert first.status_code == 200
    again = refresh(old)                       # the page reloaded mid-refresh
    assert again.status_code == 200
    assert again.data['refresh'] == first.data['refresh']
    assert again.data['access'] == first.data['access']   # nothing new minted


def test_the_successor_keeps_working(user):
    old = str(RefreshToken.for_user(user))
    new = refresh(old).data['refresh']
    refresh(old)
    assert refresh(new).status_code == 200


def test_after_the_window_the_old_token_is_refused(user):
    old = str(RefreshToken.for_user(user))
    refresh(old)
    cache.clear()                              # the 30 seconds have passed
    assert refresh(old).status_code == 401


def test_a_forged_token_is_refused_even_inside_the_window(user):
    old = str(RefreshToken.for_user(user))
    refresh(old)
    head, body, sig = old.split('.')
    forged = f'{head}.{body}.{sig[:-4]}AAAA'
    assert refresh(forged).status_code == 401


def test_logout_ends_the_grace(user):
    old = str(RefreshToken.for_user(user))
    new = refresh(old).data
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {new['access']}")
    assert c.post('/api/v1/auth/logout/', {'refresh': new['refresh']}, format='json').status_code == 205
    assert refresh(old).status_code == 401


def test_a_password_change_ends_the_grace(user):
    old = str(RefreshToken.for_user(user))
    refresh(old)
    user.set_password('n3w-Pass-2')
    user.save()
    assert refresh(old).status_code == 401
