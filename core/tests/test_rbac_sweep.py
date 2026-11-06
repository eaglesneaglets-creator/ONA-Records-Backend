"""
Task 18 — RBAC verified per endpoint, by walking the URL table.

Hand-written access tests cover the endpoints someone remembered. This one
enumerates EVERY route under /api/v1/ from Django's resolver, so an endpoint
added later without a permission class fails here on its first CI run:

  - anonymous: nothing outside PUBLIC answers 2xx, for any method
  - customer and professional: every /api/v1/admin/ route is refused (403),
    for every method — and the refusal comes before any object lookup, so a
    random id cannot be used to probe what exists
"""

import re
import uuid

import pytest
from django.urls import get_resolver
from django.urls.resolvers import URLResolver
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db

METHODS = ('get', 'post', 'put', 'patch', 'delete')

# Answer without a session — by design. Each entry is a regex on the path.
PUBLIC = [
    r'^api/v1/health/$',
    r'^api/v1/auth/(register|login|refresh|verify-email|password-reset|password-reset/confirm)/$',
    r'^api/v1/studio/(rooms|engineers|services|packages)/([0-9a-f-]+/)?$',
    r'^api/v1/studio/calendar/$',
    r'^api/v1/content/faqs/$',
]


def _routes():
    def walk(patterns, prefix=''):
        for p in patterns:
            if isinstance(p, URLResolver):
                yield from walk(p.url_patterns, prefix + str(p.pattern))
            else:
                yield prefix + str(p.pattern)

    seen = set()
    for raw in walk(get_resolver().url_patterns):
        if not raw.startswith('api/') or 'format' in raw or raw.startswith('api/v1/schema') \
                or raw.startswith('api/v1/docs'):
            continue
        path = raw.replace('^', '').replace('$', '')
        path = re.sub(r'\(\?P<\w+>[^)]+\)', str(uuid.uuid4()), path)
        path = re.sub(r'<[^>]+>', str(uuid.uuid4()), path)
        if path not in seen:
            seen.add(path)
            yield path


ROUTES = sorted(_routes())
ADMIN_ROUTES = [r for r in ROUTES if r.startswith('api/v1/admin/')]


def _public(path):
    return any(re.match(p, path) for p in PUBLIC)


def test_the_sweep_sees_the_whole_api():
    # Guard the guard: if the walk silently found nothing, every test below
    # would pass vacuously.
    assert len(ROUTES) > 40 and len(ADMIN_ROUTES) > 15


@pytest.mark.parametrize('path', [r for r in ROUTES if not _public(r)])
def test_anonymous_gets_nothing_outside_the_public_list(path):
    c = APIClient()
    for m in METHODS:
        res = getattr(c, m)('/' + path, {}, format='json')
        assert res.status_code in (401, 403, 404, 405), f'{m.upper()} /{path} → {res.status_code}'


@pytest.fixture
def people(django_user_model):
    U = django_user_model.objects
    return {
        'customer': U.create_user(email='c@x.test', password='p-Ass-1', role='customer'),
        'professional': U.create_user(email='p@x.test', password='p-Ass-1', role='professional'),
    }


@pytest.mark.parametrize('who', ['customer', 'professional'])
@pytest.mark.parametrize('path', ADMIN_ROUTES)
def test_non_staff_are_refused_on_every_staff_route(people, who, path):
    c = APIClient()
    c.force_authenticate(people[who])
    for m in METHODS:
        res = getattr(c, m)('/' + path, {}, format='json')
        assert res.status_code in (403, 405), f'{who} {m.upper()} /{path} → {res.status_code}'


def test_the_staff_api_has_no_browsable_index():
    """A signed-in customer must not be able to list the staff endpoints."""
    from django.contrib.auth import get_user_model
    c = APIClient()
    c.force_authenticate(get_user_model().objects.create_user(
        email='x@x.test', password='p-Ass-1', role='customer'))
    assert c.get('/api/v1/admin/').status_code == 404
