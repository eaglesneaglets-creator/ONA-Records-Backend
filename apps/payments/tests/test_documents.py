"""
Task 10 — invoices and receipts.

The acceptance check is literal: a generated PDF contains the booking
reference and the correct total. PDFs are rendered uncompressed so their
text can be found in the bytes without a PDF parser.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.payments.documents import DocumentError, issue_invoice, issue_receipt, next_number
from apps.payments.ledger import mark_succeeded, open_transaction
from apps.payments.models import Invoice, Receipt
from apps.payments.pdf import render_invoice, render_receipt
from apps.studio.models import Engineer, Room, Service, StudioBooking

pytestmark = pytest.mark.django_db

TZ = ZoneInfo('Africa/Accra')
_day = iter(range(10_000))


@pytest.fixture
def world(django_user_model):
    U = django_user_model.objects
    return {
        'me': U.create_user(email='abena@x.test', password='p-Ass-1', role='customer',
                            first_name='Abena', last_name='Darko'),
        'other': U.create_user(email='o@x.test', password='p-Ass-1', role='customer'),
        'admin': U.create_superuser(email='a@x.test', password='p-Ass-1'),
        'room': Room.objects.create(name='Studio A — Live Room', hourly_rate_pesewas=20000),
        'eng': Engineer.objects.create(name='Engineer One'),
        'svc': Service.objects.create(name='Recording session', base_price_pesewas=1,
                                      duration_minutes=60),
    }


def booking(w, hours=4, who='me', total=None):
    d = date.today() + timedelta(days=10 + next(_day))
    start = datetime.combine(d, datetime.min.time(), tzinfo=TZ) + timedelta(hours=10)
    return StudioBooking.objects.create(
        customer=w[who], room=w['room'], engineer=w['eng'], service=w['svc'], start_at=start,
        end_at=start + timedelta(hours=hours),
        total_pesewas=total if total is not None else 20000 * hours)


def pdf_text(content: bytes) -> str:
    return content.decode('latin-1')


class TestInvoice:
    def test_pdf_contains_the_reference_and_the_total(self, world):
        b = booking(world)                                    # 4h x GHS 200 = GHS 800
        content = render_invoice(issue_invoice(b))
        assert content.startswith(b'%PDF')
        text = pdf_text(content)
        assert b.reference in text
        assert 'GHS 800.00' in text
        assert 'Abena Darko' in text
        assert 'GHS 200.00' in text                           # the unit rate line

    def test_figures_are_frozen_at_issue(self, world):
        b = booking(world)
        inv = issue_invoice(b)
        world['room'].name = 'Renamed Room'
        world['room'].hourly_rate_pesewas = 1
        world['room'].save()
        inv.refresh_from_db()
        assert inv.total_pesewas == 80000
        assert 'Studio A' in inv.lines[0]['description']
        assert 'GHS 800.00' in pdf_text(render_invoice(inv))

    def test_one_invoice_per_booking(self, world):
        b = booking(world)
        assert issue_invoice(b).pk == issue_invoice(b).pk
        assert Invoice.objects.count() == 1

    def test_lines_always_add_up(self, world):
        # A total that does not divide by the hours gets one honest line, never
        # a rounded unit price that does not multiply back to the total.
        inv = issue_invoice(booking(world, hours=3, total=10000))
        assert sum(line['total_pesewas'] for line in inv.lines) == 10000
        assert inv.lines[0]['quantity'] == 1

    def test_numbers_are_sequential_per_year(self, world):
        a = issue_invoice(booking(world)).number
        b = issue_invoice(booking(world)).number
        year = date.today().year
        assert a.startswith(f'INV-{year}-') and b.startswith(f'INV-{year}-')
        assert int(b.rsplit('-', 1)[1]) == int(a.rsplit('-', 1)[1]) + 1

    def test_kinds_number_independently(self, world):
        assert next_number('INV').endswith('-00001')
        assert next_number('RCT').endswith('-00001')
        assert next_number('INV').endswith('-00002')

    def test_says_amount_due_until_paid_then_paid(self, world):
        b = booking(world)
        inv = issue_invoice(b)
        assert 'Amount due' in pdf_text(render_invoice(inv))
        mark_succeeded(open_transaction(b), provider_ref='HUB-1')
        assert 'Paid' in pdf_text(render_invoice(inv))

    def test_rendering_is_deterministic(self, world):
        inv = issue_invoice(booking(world))
        assert render_invoice(inv) == render_invoice(inv)


class TestReceipt:
    def test_issued_with_the_hold_and_only_once(self, world):
        b = booking(world)
        txn = open_transaction(b)
        mark_succeeded(txn, provider_ref='HUB-77')
        mark_succeeded(txn, provider_ref='HUB-77')            # replayed callback
        assert Receipt.objects.filter(transaction=txn).count() == 1
        rec = Receipt.objects.get(transaction=txn)
        assert rec.number.startswith('RCT-') and rec.amount_pesewas == 80000

    def test_pdf_contains_reference_amount_and_payment_ref(self, world):
        b = booking(world)
        txn = open_transaction(b)
        mark_succeeded(txn, provider_ref='HUB-77')
        text = pdf_text(render_receipt(txn.receipt))
        assert b.reference in text and 'GHS 800.00' in text and 'HUB-77' in text

    def test_no_receipt_for_an_unconfirmed_payment(self, world):
        txn = open_transaction(booking(world))
        with pytest.raises(DocumentError):
            issue_receipt(txn)


class TestDownloads:
    def api(self, user=None):
        c = APIClient()
        if user is not None:
            c.force_authenticate(user)
        return c

    def test_customer_downloads_their_invoice(self, world):
        b = booking(world)
        res = self.api(world['me']).get(reverse('studio:booking-invoice', args=[b.id]))
        assert res.status_code == 200
        assert res['Content-Type'] == 'application/pdf'
        assert 'attachment' in res['Content-Disposition'] and 'INV-' in res['Content-Disposition']
        assert res['Cache-Control'] == 'private, no-store'
        assert b.reference in pdf_text(res.content)

    def test_older_bookings_get_an_invoice_on_first_download(self, world):
        b = booking(world)
        assert not Invoice.objects.filter(booking=b).exists()
        self.api(world['me']).get(reverse('studio:booking-invoice', args=[b.id]))
        assert Invoice.objects.filter(booking=b).exists()

    def test_receipt_404s_until_paid(self, world):
        b = booking(world)
        url = reverse('studio:booking-receipt', args=[b.id])
        assert self.api(world['me']).get(url).status_code == 404
        mark_succeeded(open_transaction(b), provider_ref='HUB-9')
        res = self.api(world['me']).get(url)
        assert res.status_code == 200 and 'GHS 800.00' in pdf_text(res.content)

    def test_nobody_else_can_download(self, world):
        b = booking(world)
        for name in ('invoice', 'receipt'):
            url = reverse(f'studio:booking-{name}', args=[b.id])
            assert self.api(world['other']).get(url).status_code == 404
            assert self.api().get(url).status_code == 401
        assert self.api(world['admin']).get(
            reverse('studio:booking-invoice', args=[b.id])).status_code == 200

    def test_creating_a_booking_issues_its_invoice(self, world):
        start = datetime.combine(date.today() + timedelta(days=5), datetime.min.time(),
                                 tzinfo=TZ) + timedelta(hours=10)
        res = self.api(world['me']).post(reverse('studio:booking-list'), {
            'room': world['room'].id, 'engineer': world['eng'].id, 'service': world['svc'].id,
            'start_at': start.isoformat(), 'hours': 2}, format='json')
        assert res.status_code == 201
        inv = Invoice.objects.get(booking__reference=res.data['reference'])
        assert inv.total_pesewas == 40000
        assert res.data['has_receipt'] is False

    def test_list_reports_has_receipt(self, world):
        b = booking(world)
        mark_succeeded(open_transaction(b), provider_ref='HUB-5')
        rows = self.api(world['me']).get(reverse('studio:booking-list')).data['results']
        assert rows[0]['has_receipt'] is True
