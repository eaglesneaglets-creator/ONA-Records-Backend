"""
Task 13 — professional records: staff-only, private, no login in v0.1.
"""

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.marketplace.models import Professional, RateType
from apps.marketplace.serializers import ProfessionalPublicSerializer
from core.models import AuditLog

pytestmark = pytest.mark.django_db

LIST = 'adminapi:professional-list'
DETAIL = 'adminapi:professional-detail'


@pytest.fixture
def users(django_user_model):
    U = django_user_model.objects
    return {
        'admin': U.create_superuser(email='a@x.test', password='p-Ass-1'),
        'customer': U.create_user(email='c@x.test', password='p-Ass-1', role='customer'),
        'pro': U.create_user(email='p@x.test', password='p-Ass-1', role='professional'),
    }


def client(user=None):
    c = APIClient()
    if user is not None:
        c.force_authenticate(user)
    return c


@pytest.fixture
def kwesi():
    return Professional.objects.create(
        name='Kwesi Mensah', stage_name='KweSound', skills=['session guitar', 'mixing'],
        rate_type=RateType.PER_SONG, rate_pesewas=30000, phone='0244123456',
        email='kwesi@private.test', notes='Prefers evening sessions.')


class TestAccess:
    @pytest.mark.parametrize('who', ['customer', 'pro'])
    def test_non_staff_get_403_on_every_route(self, users, kwesi, who):
        c = client(users[who])
        detail = reverse(DETAIL, args=[kwesi.id])
        assert c.get(reverse(LIST)).status_code == 403
        assert c.post(reverse(LIST), {'name': 'X'}, format='json').status_code == 403
        assert c.get(detail).status_code == 403
        assert c.patch(detail, {'name': 'Y'}, format='json').status_code == 403
        assert c.delete(detail).status_code == 403

    def test_anonymous_is_401(self, kwesi):
        assert client().get(reverse(LIST)).status_code == 401

    def test_there_is_no_public_route(self, kwesi):
        # Nothing under the public API serves professional records in v0.1.
        for path in ('/api/v1/marketplace/', '/api/v1/professionals/',
                     '/api/v1/studio/professionals/'):
            assert client().get(path).status_code == 404


class TestStaffCrud:
    def test_create_list_update_and_soft_delete_are_audited(self, users):
        c = client(users['admin'])
        res = c.post(reverse(LIST), {
            'name': 'Akosua Boateng', 'skills': ['  vocals ', 'songwriting', ''],
            'rate_type': 'hourly', 'rate_pesewas': 15000, 'phone': '0200000000'}, format='json')
        assert res.status_code == 201, res.data
        assert res.data['skills'] == ['vocals', 'songwriting']
        assert res.data['rate_display'] == 'GHS 150.00 per hour'
        pid = res.data['id']

        assert c.get(reverse(LIST)).data['count'] == 1
        assert c.patch(reverse(DETAIL, args=[pid]), {'is_active': False},
                       format='json').status_code == 200
        assert c.delete(reverse(DETAIL, args=[pid])).status_code == 204
        assert Professional.objects.count() == 0
        assert Professional.all_objects.filter(pk=pid, deleted_by=users['admin']).exists()

        actions = list(AuditLog.objects.filter(actor=users['admin'])
                       .values_list('action', flat=True))
        assert {'professional.created', 'professional.updated',
                'professional.removed'} <= set(actions)

    def test_a_rate_is_required_unless_negotiable(self, users):
        c = client(users['admin'])
        assert c.post(reverse(LIST), {'name': 'A', 'rate_type': 'per_song'},
                      format='json').status_code == 400
        res = c.post(reverse(LIST), {'name': 'B', 'rate_type': 'negotiable',
                                     'rate_pesewas': 500}, format='json')
        assert res.status_code == 201 and res.data['rate_pesewas'] is None
        assert res.data['rate_display'] == 'Negotiable'

    def test_the_user_link_cannot_be_set_in_v01(self, users):
        """ADR-016: only the v0.2 claim flow may link a login to a record."""
        c = client(users['admin'])
        res = c.post(reverse(LIST), {'name': 'C', 'user': str(users['pro'].id)}, format='json')
        assert res.status_code == 201
        assert Professional.objects.get(pk=res.data['id']).user is None

    def test_search(self, users, kwesi):
        Professional.objects.create(name='Other Person')
        res = client(users['admin']).get(reverse(LIST), {'search': 'kwesound'})
        assert [r['name'] for r in res.data['results']] == ['Kwesi Mensah']


class TestPrivacy:
    def test_the_customer_facing_view_has_no_contact_details(self, kwesi):
        data = ProfessionalPublicSerializer(kwesi).data
        assert data == {'id': str(kwesi.id), 'display_name': 'KweSound',
                        'skills': ['session guitar', 'mixing']}
        flat = str(data)
        for secret in ('0244123456', 'kwesi@private.test', 'evening', 'Mensah'):
            assert secret not in flat
