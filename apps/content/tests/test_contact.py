"""Contact details: staff keep them, the public page shows them, launch_check insists on them."""

import pytest
from rest_framework.test import APIClient

from apps.content.models import ContactDetails
from apps.studio.management.commands.launch_check import collect
from core.models import AuditLog

pytestmark = pytest.mark.django_db
PUBLIC = '/api/v1/content/contact/'
ADMIN = '/api/v1/admin/contact/'


@pytest.fixture
def staff(django_user_model):
    c = APIClient()
    c.force_authenticate(django_user_model.objects.create_superuser(email='s@x.test', password='x'))
    return c


def test_public_page_gets_blank_fields_until_staff_fill_them():
    r = APIClient().get(PUBLIC)
    assert r.status_code == 200
    assert r.json() == {'phone': '', 'whatsapp': '', 'email': '', 'address': '',
                        'directions_url': ''}


def test_staff_update_is_validated_and_audited(staff):
    bad = staff.patch(ADMIN, {'phone': 'ring us'}, format='json')
    assert bad.status_code == 400 and 'phone' in bad.json()['error']['detail']

    r = staff.patch(ADMIN, {'phone': '030 000 0000', 'email': 'hello@ona.test',
                            'address': ' East Legon, Accra '}, format='json')
    assert r.status_code == 200, r.content
    assert APIClient().get(PUBLIC).json()['address'] == 'East Legon, Accra'
    entry = AuditLog.objects.get(action=AuditLog.Action.CONTACT_UPDATED)
    assert entry.metadata['changes']['phone'] == {'from': '', 'to': '030 000 0000'}


def test_customers_cannot_edit(django_user_model):
    c = APIClient()
    c.force_authenticate(django_user_model.objects.create_user(email='c@x.test', password='x'))
    assert c.patch(ADMIN, {'phone': '030 000 0000'}, format='json').status_code == 403


def test_empty_contact_details_block_launch():
    assert any('Contact details are empty' in m for lvl, m in collect() if lvl == 'BLOCKER')
    ContactDetails.objects.filter(pk=1).update(email='hello@ona.test')
    assert not any('Contact details' in m for lvl, m in collect())
