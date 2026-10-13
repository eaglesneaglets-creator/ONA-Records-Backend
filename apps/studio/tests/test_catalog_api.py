"""
Public catalogue API.

Two things matter: anyone can read it (no token), and nothing a customer
cannot book is visible through it — not in the list, not by direct id.
"""

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.studio.models import Engineer, Room, Service

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def catalogue():
    return {
        'room': Room.objects.create(name='Live Room', hourly_rate_pesewas=20000, capacity=8),
        'engineer': Engineer.objects.create(name='Ama', specialties=['mixing']),
        'service': Service.objects.create(name='Mixing', base_price_pesewas=30000,
                                          duration_minutes=180),
    }


class TestPublicRead:
    @pytest.mark.parametrize('route', ['studio:room-list', 'studio:engineer-list',
                                       'studio:service-list'])
    def test_anonymous_can_list(self, api, catalogue, route):
        res = api.get(reverse(route))
        assert res.status_code == 200
        assert res.data['count'] == 1
        assert {'count', 'next', 'previous', 'results'} <= set(res.data)

    def test_anonymous_can_read_detail(self, api, catalogue):
        res = api.get(reverse('studio:room-detail', args=[catalogue['room'].id]))
        assert res.status_code == 200
        assert res.data['name'] == 'Live Room'

    def test_money_ships_as_pesewas_and_display(self, api, catalogue):
        room = api.get(reverse('studio:room-list')).data['results'][0]
        assert room['hourly_rate_pesewas'] == 20000
        assert room['hourly_rate_display'] == 'GHS 200.00'

        svc = api.get(reverse('studio:service-list')).data['results'][0]
        assert svc['base_price_pesewas'] == 30000
        assert svc['base_price_display'] == 'GHS 300.00'
        assert svc['duration_minutes'] == 180

    def test_internal_fields_do_not_leak(self, api, catalogue):
        room = api.get(reverse('studio:room-list')).data['results'][0]
        for hidden in ('deleted_at', 'deleted_by', 'sort_order', 'is_active'):
            assert hidden not in room


class TestVisibility:
    def test_inactive_rows_are_absent_from_list(self, api, catalogue):
        Room.objects.create(name='Closed', hourly_rate_pesewas=100, is_active=False)
        names = [r['name'] for r in api.get(reverse('studio:room-list')).data['results']]
        assert names == ['Live Room']

    def test_inactive_row_is_404_by_id(self, api, catalogue):
        closed = Room.objects.create(name='Closed', hourly_rate_pesewas=100, is_active=False)
        assert api.get(reverse('studio:room-detail', args=[closed.id])).status_code == 404

    def test_soft_deleted_row_is_404_by_id(self, api, catalogue):
        room = catalogue['room']
        room.soft_delete()
        assert api.get(reverse('studio:room-detail', args=[room.id])).status_code == 404
        assert api.get(reverse('studio:room-list')).data['count'] == 0


class TestReadOnly:
    @pytest.mark.parametrize('method', ['post', 'put', 'patch', 'delete'])
    def test_writes_are_rejected_even_for_admin(self, api, catalogue, method, django_user_model):
        admin = django_user_model.objects.create_superuser(email='a@ona.test', password='x-Pass-1')
        api.force_authenticate(admin)
        list_url = reverse('studio:room-list')
        detail_url = reverse('studio:room-detail', args=[catalogue['room'].id])
        url = list_url if method == 'post' else detail_url
        res = getattr(api, method)(url, {'name': 'X'}, format='json')
        assert res.status_code == 405


class TestOrderingAndSearch:
    def test_list_follows_sort_order_then_name(self, api):
        Room.objects.create(name='B', hourly_rate_pesewas=1, sort_order=10)
        Room.objects.create(name='A', hourly_rate_pesewas=1, sort_order=10)
        Room.objects.create(name='Z', hourly_rate_pesewas=1, sort_order=1)
        names = [r['name'] for r in api.get(reverse('studio:room-list')).data['results']]
        assert names == ['Z', 'A', 'B']

    def test_search_by_name(self, api, catalogue):
        Room.objects.create(name='Vocal Booth', hourly_rate_pesewas=1)
        res = api.get(reverse('studio:room-list'), {'search': 'vocal'})
        assert [r['name'] for r in res.data['results']] == ['Vocal Booth']
