"""C6 — a customer's own payment history and receipts."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from rest_framework.test import APIClient

from apps.payments.ledger import mark_succeeded, open_transaction, refund
from apps.studio.models import Engineer, Room, Service, StudioBooking

pytestmark = pytest.mark.django_db
TZ = ZoneInfo('Africa/Accra')
_day = iter(range(10_000))
LIST = '/api/v1/payments/mine/'


@pytest.fixture
def world(django_user_model):
    U = django_user_model.objects
    return {
        'me': U.create_user(email='abena@x.test', password='p-Ass-1', role='customer'),
        'other': U.create_user(email='o@x.test', password='p-Ass-1', role='customer'),
        'room': Room.objects.create(name='Live Room', hourly_rate_pesewas=20000),
        'eng': Engineer.objects.create(name='Engineer One'),
        'svc': Service.objects.create(name='Recording', base_price_pesewas=0, duration_minutes=60),
    }


def booking(w, who='me'):
    d = date.today() + timedelta(days=10 + next(_day))
    start = datetime.combine(d, datetime.min.time(), tzinfo=TZ) + timedelta(hours=10)
    return StudioBooking.objects.create(
        customer=w[who], room=w['room'], engineer=w['eng'], service=w['svc'], start_at=start,
        end_at=start + timedelta(hours=2), total_pesewas=40000)


def as_user(user) -> APIClient:
    c = APIClient()
    c.force_authenticate(user)
    return c


def test_lists_only_my_payments_newest_first_with_refunds(world):
    paid = open_transaction(booking(world))
    mark_succeeded(paid, provider_ref='PS-1')
    refund(paid, 10000)
    open_transaction(booking(world))                       # started, not finished
    mark_succeeded(open_transaction(booking(world, 'other')), provider_ref='PS-9')

    rows = as_user(world['me']).get(LIST).json()['results']
    assert len(rows) == 2
    newest, first = rows
    assert newest['status'] == 'pending' and newest['receipt_number'] is None
    assert first['status'] == 'succeeded'
    assert first['amount_display'] == 'GHS 400.00'
    assert first['refunded_display'] == 'GHS 100.00'
    assert first['how'] == 'Paid online (Paystack)'
    assert first['receipt_number']
    assert first['booking']['room'] == 'Live Room'


def test_receipt_for_a_confirmed_payment(world):
    txn = open_transaction(booking(world))
    mark_succeeded(txn, provider_ref='PS-2')
    r = as_user(world['me']).get(f'{LIST}{txn.pk}/receipt/')
    assert r.status_code == 200
    assert r['Content-Type'] == 'application/pdf'
    assert r['Cache-Control'] == 'private, no-store'


def test_no_receipt_before_payment_and_none_for_someone_else(world):
    pending = open_transaction(booking(world))
    theirs = open_transaction(booking(world, 'other'))
    mark_succeeded(theirs, provider_ref='PS-3')
    me = as_user(world['me'])
    assert me.get(f'{LIST}{pending.pk}/receipt/').status_code == 404
    assert me.get(f'{LIST}{theirs.pk}/receipt/').status_code == 404
    assert me.get(f'{LIST}{theirs.pk}/').status_code == 404


def test_requires_sign_in_and_is_read_only(world):
    assert APIClient().get(LIST).status_code == 401
    assert as_user(world['me']).post(LIST, {}).status_code == 405
