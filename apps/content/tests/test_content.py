"""
Task 16 — content and catalogue editable by staff: FAQs (public page reads
them), rooms/services/engineers/packages with audited price changes.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.content.models import Faq
from apps.studio.models import Engineer, Package, Room, Service, StudioBooking
from core.models import AuditLog

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(django_user_model):
    c = APIClient()
    c.force_authenticate(django_user_model.objects.create_user(
        email='staff@x.test', password='p-Ass-1', role='admin'))
    return c


@pytest.fixture
def customer(django_user_model):
    c = APIClient()
    c.force_authenticate(django_user_model.objects.create_user(
        email='c@x.test', password='p-Ass-1', role='customer'))
    return c


class TestFaqs:
    def test_seeded_from_the_landing_page(self):
        assert Faq.objects.count() == 6
        assert Faq.objects.first().question == 'Is an engineer included?'

    def test_an_edited_answer_appears_publicly_and_is_audited(self, staff):
        faq = Faq.objects.get(question='How do I pay?')
        res = staff.patch(reverse('adminapi:faq-detail', args=[faq.pk]),
                          {'answer': 'Mobile Money only for now.'}, format='json')
        assert res.status_code == 200, res.data
        public = APIClient().get(reverse('content:faq-list')).data
        assert {'id': str(faq.pk), 'question': 'How do I pay?',
                'answer': 'Mobile Money only for now.'} in public
        log = AuditLog.objects.get(action='content.faq_updated')
        assert log.metadata['changes']['answer']['to'] == 'Mobile Money only for now.'

    def test_unpublished_answers_stay_off_the_public_page(self, staff):
        faq = Faq.objects.first()
        staff.patch(reverse('adminapi:faq-detail', args=[faq.pk]), {'is_published': False},
                    format='json')
        public = APIClient().get(reverse('content:faq-list')).data
        assert faq.question not in [f['question'] for f in public] and len(public) == 5

    def test_create_and_delete_are_audited(self, staff):
        res = staff.post(reverse('adminapi:faq-list'),
                         {'question': ' Parking? ', 'answer': 'Free on site.', 'sort_order': 5},
                         format='json')
        assert res.status_code == 201 and res.data['question'] == 'Parking?'
        assert APIClient().get(reverse('content:faq-list')).data[0]['question'] == 'Parking?'
        assert staff.delete(reverse('adminapi:faq-detail', args=[res.data['id']])).status_code == 204
        assert {'content.faq_created', 'content.faq_deleted'} <= set(
            AuditLog.objects.values_list('action', flat=True))

    def test_an_answer_is_required(self, staff):
        res = staff.post(reverse('adminapi:faq-list'), {'question': 'Q', 'answer': '  '},
                         format='json')
        assert res.status_code == 400

    def test_customers_cannot_edit(self, customer):
        faq = Faq.objects.first()
        assert customer.patch(reverse('adminapi:faq-detail', args=[faq.pk]), {'answer': 'x'},
                              format='json').status_code == 403
        assert customer.get(reverse('adminapi:faq-list')).status_code == 403


class TestCatalogue:
    def test_a_price_change_is_audited_and_never_reprices_a_booking(
            self, staff, django_user_model):
        room = Room.objects.create(name='Live Room', hourly_rate_pesewas=20000)
        start = timezone.now() + timedelta(days=5)
        b = StudioBooking.objects.create(
            customer=django_user_model.objects.create_user(email='a@x.test', role='customer'),
            room=room, engineer=Engineer.objects.create(name='One'),
            service=Service.objects.create(name='Rec', base_price_pesewas=1, duration_minutes=60),
            start_at=start, end_at=start + timedelta(hours=2), total_pesewas=40000)

        res = staff.patch(reverse('adminapi:room-detail', args=[room.pk]),
                          {'hourly_rate_pesewas': 25000}, format='json')
        assert res.status_code == 200 and res.data['hourly_rate_display'] == 'GHS 250.00'
        b.refresh_from_db()
        assert b.total_pesewas == 40000
        log = AuditLog.objects.get(action='catalogue.updated')
        assert log.metadata['changes'] == {'hourly_rate_pesewas': {'from': 20000, 'to': 25000}}
        # The public catalogue shows the new rate.
        public = APIClient().get(reverse('studio:room-list')).data['results']
        assert public[0]['hourly_rate_pesewas'] == 25000

    def test_hidden_items_are_off_the_public_catalogue(self, staff):
        svc = Service.objects.create(name='Mixing', base_price_pesewas=30000, duration_minutes=60)
        staff.patch(reverse('adminapi:service-detail', args=[svc.pk]), {'is_active': False},
                    format='json')
        assert APIClient().get(reverse('studio:service-list')).data['count'] == 0
        assert staff.get(reverse('adminapi:service-list')).data['count'] == 1

    def test_names_are_unique_in_words(self, staff):
        Room.objects.create(name='Live Room', hourly_rate_pesewas=1)
        res = staff.post(reverse('adminapi:room-list'), {'name': 'live room',
                                                         'hourly_rate_pesewas': 100}, format='json')
        assert res.status_code == 400 and 'already has this name' in str(res.data)

    def test_remove_is_a_soft_delete(self, staff):
        eng = Engineer.objects.create(name='Two')
        assert staff.delete(reverse('adminapi:engineer-detail', args=[eng.pk])).status_code == 204
        assert not Engineer.objects.filter(pk=eng.pk).exists()
        assert Engineer.all_objects.filter(pk=eng.pk).exists()
        assert AuditLog.objects.filter(action='catalogue.removed').exists()

    def test_engineer_specialties_are_cleaned(self, staff):
        res = staff.post(reverse('adminapi:engineer-list'),
                         {'name': 'Three', 'specialties': [' mixing ', '']}, format='json')
        assert res.status_code == 201 and res.data['specialties'] == ['mixing']
        assert staff.post(reverse('adminapi:engineer-list'),
                          {'name': 'Four', 'specialties': 'mixing'}, format='json').status_code == 400

    def test_packages_are_created_and_listed_publicly(self, staff):
        room = Room.objects.create(name='Booth', hourly_rate_pesewas=12000)
        res = staff.post(reverse('adminapi:package-list'), {
            'name': 'Single, recorded and mixed', 'hours': 4, 'price_pesewas': 60000,
            'room': str(room.pk), 'includes': ['Engineer', ' Two mix revisions ', '']},
            format='json')
        assert res.status_code == 201, res.data
        assert res.data['price_display'] == 'GHS 600.00'
        [pkg] = APIClient().get(reverse('studio:package-list')).data['results']
        assert pkg['room_name'] == 'Booth' and pkg['includes'] == ['Engineer', 'Two mix revisions']
        assert AuditLog.objects.get(action='catalogue.created').metadata['kind'] == 'package'

    def test_package_includes_must_be_a_list(self, staff):
        res = staff.post(reverse('adminapi:package-list'), {
            'name': 'X', 'hours': 1, 'price_pesewas': 1, 'includes': 'nope'}, format='json')
        assert res.status_code == 400

    def test_customers_cannot_edit_prices(self, customer):
        room = Room.objects.create(name='R', hourly_rate_pesewas=1)
        assert customer.patch(reverse('adminapi:room-detail', args=[room.pk]),
                              {'hourly_rate_pesewas': 2}, format='json').status_code == 403
        assert Package.objects.count() == 0
