"""Business settings: staff-editable rules, the public read, tax on invoices."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from rest_framework.test import APIClient

from apps.payments.checkout import deposit_for
from apps.payments.documents import issue_invoice
from apps.payments.pdf import render_invoice
from apps.studio.management.commands.launch_check import collect
from apps.studio.models import Engineer, Room, Service, StudioBooking
from core.business import percent_text, rules, tax_breakdown
from core.models import AuditLog

pytestmark = pytest.mark.django_db
ADMIN = '/api/v1/admin/business/'
PUBLIC = '/api/v1/content/rules/'
TZ = ZoneInfo('Africa/Accra')
_day = iter(range(10_000))
GHANA = [{'name': 'VAT', 'basis_points': 1500}, {'name': 'NHIL', 'basis_points': 250},
         {'name': 'GETFund', 'basis_points': 250}]


@pytest.fixture
def staff(django_user_model):
    c = APIClient()
    c.force_authenticate(django_user_model.objects.create_superuser(email='boss@ona.test', password='x'))
    return c


def errors(r) -> dict:
    return r.json()['error']['detail']


def booking(django_user_model, total=12000):
    me = django_user_model.objects.create_user(email=f'c{next(_day)}@x.test', password='x')
    d = date.today() + timedelta(days=10 + next(_day))
    start = datetime.combine(d, datetime.min.time(), tzinfo=TZ) + timedelta(hours=10)
    return StudioBooking.objects.create(
        customer=me, room=Room.objects.create(name=f'Room {next(_day)}', hourly_rate_pesewas=total),
        engineer=Engineer.objects.create(name=f'E{next(_day)}'),
        service=Service.objects.create(name=f'S{next(_day)}', base_price_pesewas=0, duration_minutes=60),
        start_at=start, end_at=start + timedelta(hours=1), total_pesewas=total)


class TestRules:
    def test_environment_applies_until_staff_save(self, settings):
        settings.BOOKING_DEPOSIT_PERCENT = 40
        r = rules()
        assert r.saved is False and r.booking_deposit_percent == 40 and r.active_taxes == []

    def test_public_read_shows_what_the_site_needs(self):
        body = APIClient().get(PUBLIC).json()
        assert body['booking_deposit_percent'] == 50 and body['taxes'] == []
        assert 'invoice_bank_details' not in body and 'studio_commission_percent' not in body

    def test_staff_change_takes_effect_and_is_audited(self, staff, django_user_model):
        b = booking(django_user_model, total=10000)
        assert deposit_for(b) == 5000
        r = staff.patch(ADMIN, {'booking_deposit_percent': 30, 'unpaid_hold_minutes': 120}, format='json')
        assert r.status_code == 200, r.content
        assert r.json()['saved'] is True and r.json()['updated_by'] == 'boss@ona.test'
        assert deposit_for(b) == 3000
        assert rules().unpaid_hold_minutes == 120
        assert rules().free_cancellation_hours == 48          # untouched: kept the environment value
        entry = AuditLog.objects.get(action=AuditLog.Action.BUSINESS_SETTINGS_UPDATED)
        assert entry.metadata['changes']['booking_deposit_percent'] == {'from': 50, 'to': 30}

    @pytest.mark.parametrize('body, field', [
        ({'studio_open_hour': 22, 'studio_close_hour': 9}, 'studio_close_hour'),
        ({'tax_enabled': True}, 'taxes'),
        ({'taxes': [{'name': 'VAT', 'basis_points': 1500}, {'name': 'vat', 'basis_points': 100}]}, 'taxes'),
        ({'booking_deposit_percent': 0}, 'booking_deposit_percent'),
        ({'unpaid_hold_minutes': 5}, 'unpaid_hold_minutes'),
        ({'invoice_business_name': '  '}, 'invoice_business_name'),
    ])
    def test_invalid_changes_are_refused_on_the_field(self, staff, body, field):
        r = staff.patch(ADMIN, body, format='json')
        assert r.status_code == 400 and field in errors(r)
        assert rules().saved is False

    def test_customers_cannot_read_or_change(self, django_user_model):
        c = APIClient()
        c.force_authenticate(django_user_model.objects.create_user(email='c@x.test', password='x'))
        assert c.get(ADMIN).status_code == 403
        assert c.patch(ADMIN, {'booking_deposit_percent': 1}, format='json').status_code == 403


class TestTax:
    def test_breakdown_of_an_inclusive_total(self):
        net, parts = tax_breakdown(12000, GHANA)
        assert net == 10000 and [p['amount_pesewas'] for p in parts] == [1500, 250, 250]
        net, parts = tax_breakdown(9999, GHANA)
        assert net + sum(p['amount_pesewas'] for p in parts) == 9999

    def test_percent_text(self):
        assert [percent_text(b) for b in (1500, 250, 1250, 5)] == ['15%', '2.5%', '12.5%', '0.05%']

    def test_invoice_freezes_the_rates_it_was_issued_with(self, staff, django_user_model):
        staff.patch(ADMIN, {'taxes': GHANA, 'tax_enabled': True, 'invoice_tin': 'C0000000000'},
                    format='json')
        inv = issue_invoice(booking(django_user_model, total=12000))
        assert inv.tax_rates == GHANA and inv.total_pesewas == 12000   # prices include tax
        # PDF strings escape parentheses: VAT \(15%\).
        text = render_invoice(inv).decode('latin-1').replace(r'\(', '(').replace(r'\)', ')')
        assert 'TAX INVOICE' in text and 'VAT (15%)' in text and 'NHIL (2.5%)' in text
        assert 'GHS 100.00' in text                                     # amount before tax

        staff.patch(ADMIN, {'tax_enabled': False}, format='json')
        inv.refresh_from_db()
        assert inv.tax_rates == GHANA                                   # issued: unchanged
        later = issue_invoice(booking(django_user_model))
        assert later.tax_rates == [] and 'TAX INVOICE' not in render_invoice(later).decode('latin-1')

    def test_launch_check_wants_a_tin_when_tax_is_on(self, staff):
        staff.patch(ADMIN, {'taxes': GHANA, 'tax_enabled': True}, format='json')
        assert any('no TIN' in m for lvl, m in collect() if lvl == 'BLOCKER')
