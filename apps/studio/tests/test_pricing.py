"""
Pricing by service (apps/studio/pricing.py) — ONA's model: the room carries no
rate, each service is priced per hour, 8-hour day, song or track.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from rest_framework.test import APIClient

from apps.studio.models import Engineer, PriceUnit, Room, Service, StudioBooking
from apps.studio.pricing import PricingError, invoice_lines, quote

pytestmark = pytest.mark.django_db
TZ = ZoneInfo('Africa/Accra')


@pytest.fixture
def studio_one():
    return Room.objects.create(name='Studio One', capacity=30)  # no hourly rate


def svc(name, pesewas, unit, minutes=60):
    return Service.objects.create(name=name, base_price_pesewas=pesewas, price_unit=unit,
                                  duration_minutes=minutes)


class TestQuote:
    def test_hourly(self, studio_one):
        q = quote(studio_one, svc('Rehearsal Space', 15000, PriceUnit.HOUR), 3)
        assert q.total_pesewas == 45000 and q.quantity == 1 and q.room_pesewas == 0

    def test_day_pass_is_one_price_for_eight_hours(self, studio_one):
        q = quote(studio_one, svc('Full Day Studio Pass', 100000, PriceUnit.DAY, 480), 8)
        assert q.total_pesewas == 100000

    @pytest.mark.parametrize('hours', [1, 7, 9])
    def test_day_pass_refuses_part_days(self, studio_one, hours):
        with pytest.raises(PricingError) as exc:
            quote(studio_one, svc('Full Day Studio Pass', 100000, PriceUnit.DAY, 480), hours)
        assert exc.value.field == 'hours'

    def test_per_song_charges_songs_not_hours(self, studio_one):
        q = quote(studio_one, svc('Digital Production', 400000, PriceUnit.SONG), 5, quantity=2)
        assert q.total_pesewas == 800000 and q.quantity == 2

    @pytest.mark.parametrize('n', [0, 21])
    def test_per_track_quantity_is_bounded(self, studio_one, n):
        with pytest.raises(PricingError) as exc:
            quote(studio_one, svc('Mixing & Mastering', 100000, PriceUnit.TRACK), 1, quantity=n)
        assert exc.value.field == 'quantity'

    def test_hourly_ignores_a_stray_quantity(self, studio_one):
        q = quote(studio_one, svc('Overdub', 30000, PriceUnit.HOUR), 2, quantity=4)
        assert q.total_pesewas == 60000 and q.quantity == 1

    def test_room_hire_is_added_when_a_room_has_a_rate(self):
        room = Room.objects.create(name='B', hourly_rate_pesewas=5000)
        q = quote(room, svc('Overdub', 30000, PriceUnit.HOUR), 2)
        assert (q.service_pesewas, q.room_pesewas, q.total_pesewas) == (60000, 10000, 70000)


class TestInvoiceLines:
    def test_lines_add_up(self, studio_one):
        for service, hours, n in (
            (svc('Rehearsal', 15000, PriceUnit.HOUR), 3, 1),
            (svc('Day', 100000, PriceUnit.DAY, 480), 8, 1),
            (svc('Mix', 100000, PriceUnit.TRACK), 2, 3),
        ):
            q = quote(studio_one, service, hours, n)
            lines = invoice_lines(studio_one, service, q)
            assert sum(line['total_pesewas'] for line in lines) == q.total_pesewas
            assert all(line['quantity'] * line['unit_pesewas'] == line['total_pesewas']
                       for line in lines)

    def test_per_song_line_says_studio_time_is_included(self, studio_one):
        s = svc('Digital Production', 400000, PriceUnit.SONG)
        line, = invoice_lines(studio_one, s, quote(studio_one, s, 4, 1))
        assert 'per song' in line['description'] and '4 h studio time included' in line['description']


class TestBookingApi:
    @pytest.fixture
    def customer(self, django_user_model):
        u = django_user_model.objects.create_user(email='c@x.test', password='p-Ass-1',
                                                  role='customer')
        c = APIClient()
        c.force_authenticate(u)
        return c

    def start(self):
        day = date.today() + timedelta(days=3)
        return (datetime.combine(day, datetime.min.time(), tzinfo=TZ) + timedelta(hours=10)).isoformat()

    def test_per_song_booking_is_priced_by_songs_and_frozen(self, customer, studio_one):
        eng = Engineer.objects.create(name='Michael Kontor')
        s = svc('Music Video Shoot (Miming)', 400000, PriceUnit.SONG)
        res = customer.post('/api/v1/studio/bookings/', {
            'room': str(studio_one.pk), 'engineer': str(eng.pk), 'service': str(s.pk),
            'start_at': self.start(), 'hours': 4, 'quantity': 2}, format='json')
        assert res.status_code == 201, res.data
        assert res.data['total_pesewas'] == 800000 and res.data['quantity'] == 2
        b = StudioBooking.objects.get(pk=res.data['id'])
        assert b.invoice.lines[0]['quantity'] == 2

    def test_day_pass_for_four_hours_is_refused_on_the_field(self, customer, studio_one):
        eng = Engineer.objects.create(name='Michael Kontor')
        s = svc('Full Day Studio Pass', 100000, PriceUnit.DAY, 480)
        res = customer.post('/api/v1/studio/bookings/', {
            'room': str(studio_one.pk), 'engineer': str(eng.pk), 'service': str(s.pk),
            'start_at': self.start(), 'hours': 4}, format='json')
        assert res.status_code == 400 and 'hours' in res.data['error']['detail']

    def test_catalogue_says_what_a_price_buys(self, studio_one):
        svc('Mixing & Mastering', 100000, PriceUnit.TRACK)
        data = APIClient().get('/api/v1/studio/services/').data['results'][0]
        assert data['price_unit'] == 'track' and data['unit_label'] == 'per track'
        room = APIClient().get('/api/v1/studio/rooms/').data['results'][0]
        assert room['hourly_rate_pesewas'] is None and room['hourly_rate_display'] == ''
