"""
Tasks 8 and 9 on Paystack (ADR-018): checkout, the webhook, the return page
and the reconcile sweep. Paystack is mocked at the client boundary
(paystack.initialize / paystack.verify); the client itself is tested in
test_paystack.py.

The non-negotiables, from the plan:
  * replaying the same webhook gives exactly ONE hold and ONE confirmation;
  * a success arriving after we marked the attempt failed is still recorded
    — the money is real;
  * nothing is trusted without Paystack's verify: not an unsigned webhook,
    not a visit to the return page, not a wrong amount.
"""

import hashlib
import hmac
import json
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.payments import checkout, paystack
from apps.payments.models import EntryType, LedgerEntry, Provider, Transaction, TransactionStatus
from apps.studio.models import BookingStatus, Engineer, Room, Service, StudioBooking
from core.models import AuditLog

pytestmark = pytest.mark.django_db

TZ = ZoneInfo('Africa/Accra')
KEY = 'sk_test_ona'
WEBHOOK = '/api/v1/payments/paystack/webhook/'


@pytest.fixture(autouse=True)
def key(settings):
    settings.PAYSTACK_SECRET_KEY = KEY
    settings.FRONTEND_URL = 'https://ona.test'
    settings.PAYSTACK_CALLBACK_URL = ''


@pytest.fixture
def world(django_user_model):
    return {
        'customer': django_user_model.objects.create_user(email='c@x.test', password='p-Ass-1'),
        'other': django_user_model.objects.create_user(email='o@x.test', password='p-Ass-1'),
        'room': Room.objects.create(name='A', hourly_rate_pesewas=20000),
        'eng': Engineer.objects.create(name='One'),
        'svc': Service.objects.create(name='Rec', base_price_pesewas=1, duration_minutes=60),
    }


_slot = iter(range(10_000))


def make_booking(w, total=40000, **kw):
    day = date.today() + timedelta(days=5 + next(_slot))
    start = datetime.combine(day, datetime.min.time(), tzinfo=TZ) + timedelta(hours=10)
    return StudioBooking.objects.create(
        customer=w['customer'], room=w['room'], engineer=w['eng'], service=w['svc'],
        start_at=start, end_at=start + timedelta(hours=2), total_pesewas=total, **kw)


@pytest.fixture
def booking(world):
    return make_booking(world)


def client_for(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def init_ok(**kw):
    return {'authorization_url': 'https://checkout.paystack.com/abc', 'access_code': 'abc',
            'reference': kw.get('reference')}


def start(booking, user=None):
    with mock.patch.object(paystack, 'initialize', side_effect=init_ok) as init:
        url, ref = checkout.start_checkout(booking, user or booking.customer)
    return ref, init


def paid(txn, **over):
    return {'status': 'success', 'amount': txn.amount_pesewas, 'currency': 'GHS',
            'channel': 'mobile_money', **over}


def signed_post(event):
    raw = json.dumps(event).encode()
    sig = hmac.new(KEY.encode(), raw, hashlib.sha512).hexdigest()
    return APIClient().post(WEBHOOK, data=raw, content_type='application/json',
                            HTTP_X_PAYSTACK_SIGNATURE=sig)


def charge_success(ref):
    return {'event': 'charge.success', 'data': {'reference': ref, 'status': 'success'}}


# ---------------------------------------------------------------------------
# Checkout (Task 8)
# ---------------------------------------------------------------------------

class TestCheckout:
    def test_opens_a_pending_paystack_transaction_for_what_is_owed(self, booking):
        ref, init = start(booking)
        txn = Transaction.objects.get(provider_ref=ref)
        assert txn.provider == Provider.PAYSTACK and txn.status == TransactionStatus.PENDING
        assert txn.amount_pesewas == 40000
        sent = init.call_args.kwargs
        assert sent['amount_pesewas'] == 40000 and sent['email'] == 'c@x.test'
        assert sent['callback_url'] == 'https://ona.test/customer/payments/return'
        assert ref.startswith(booking.reference + '-')
        # Never confirmed optimistically: nothing is held, the booking waits.
        assert not LedgerEntry.objects.filter(transaction=txn).exists()
        booking.refresh_from_db()
        assert booking.status == BookingStatus.PENDING

    def test_charges_only_the_balance_after_a_part_payment_by_hand(self, booking, world):
        from apps.payments.manual import record_payment
        record_payment(booking, 15000, 'cash', '', '', actor=None)
        ref, init = start(booking)
        assert init.call_args.kwargs['amount_pesewas'] == 25000

    def test_each_attempt_has_its_own_reference(self, booking):
        a, _ = start(booking)
        b, _ = start(booking)
        assert a != b

    def test_refused_when_paid_in_full(self, booking):
        from apps.payments.manual import record_payment
        record_payment(booking, 40000, 'cash', '', '', actor=None)
        with pytest.raises(checkout.CheckoutRefused, match='already paid'):
            start(booking)

    def test_refused_when_cancelled(self, booking):
        booking.status = BookingStatus.CANCELLED
        booking.save()
        with pytest.raises(checkout.CheckoutRefused, match='cancelled'):
            start(booking)

    def test_refused_when_paystack_is_off(self, booking, settings):
        settings.PAYSTACK_SECRET_KEY = ''
        with pytest.raises(checkout.CheckoutRefused, match='not available'):
            start(booking)

    def test_a_checkout_that_never_started_is_closed_as_failed(self, booking):
        with mock.patch.object(paystack, 'initialize',
                               side_effect=paystack.PaystackError('down')):
            with pytest.raises(paystack.PaystackError):
                checkout.start_checkout(booking, booking.customer)
        assert Transaction.objects.get(booking=booking).status == TransactionStatus.FAILED

    def test_endpoint(self, booking):
        with mock.patch.object(paystack, 'initialize', side_effect=init_ok):
            res = client_for(booking.customer).post('/api/v1/payments/checkout/',
                                                    {'booking': str(booking.pk)}, format='json')
        assert res.status_code == 201
        assert res.data['authorization_url'] == 'https://checkout.paystack.com/abc'

    def test_endpoint_hides_other_peoples_bookings(self, booking, world):
        res = client_for(world['other']).post('/api/v1/payments/checkout/',
                                              {'booking': str(booking.pk)}, format='json')
        assert res.status_code == 404
        assert not Transaction.objects.exists()

    def test_endpoint_reports_a_refusal_as_400(self, booking, settings):
        settings.PAYSTACK_SECRET_KEY = ''
        res = client_for(booking.customer).post('/api/v1/payments/checkout/',
                                                {'booking': str(booking.pk)}, format='json')
        assert res.status_code == 400 and 'not available' in res.data['detail']


# ---------------------------------------------------------------------------
# Settling (Task 9)
# ---------------------------------------------------------------------------

class TestSettle:
    def test_success_holds_the_money_and_confirms_the_booking(self, booking):
        ref, _ = start(booking)
        txn = Transaction.objects.get(provider_ref=ref)
        with mock.patch.object(paystack, 'verify', return_value=paid(txn)):
            checkout.settle(ref)
        txn.refresh_from_db()
        booking.refresh_from_db()
        assert txn.status == TransactionStatus.SUCCEEDED
        assert LedgerEntry.objects.get(transaction=txn).entry_type == EntryType.HOLD
        assert booking.status == BookingStatus.CONFIRMED
        assert hasattr(txn, 'receipt')
        actions = set(AuditLog.objects.values_list('action', flat=True))
        assert {'payment.succeeded', 'booking.confirmed'} <= actions

    def test_part_of_the_total_does_not_confirm(self, world):
        b = make_booking(world)
        ref, _ = start(b)
        txn = Transaction.objects.get(provider_ref=ref)
        # Staff added a professional's fee after the checkout started.
        StudioBooking.objects.filter(pk=b.pk).update(total_pesewas=60000)
        with mock.patch.object(paystack, 'verify', return_value=paid(txn)):
            checkout.settle(ref)
        b.refresh_from_db()
        assert b.status == BookingStatus.PENDING

    def test_wrong_amount_is_left_for_a_person(self, booking):
        ref, _ = start(booking)
        txn = Transaction.objects.get(provider_ref=ref)
        with mock.patch.object(paystack, 'verify', return_value=paid(txn, amount=100)):
            checkout.settle(ref)
        txn.refresh_from_db()
        assert txn.status == TransactionStatus.PENDING
        assert not LedgerEntry.objects.filter(transaction=txn).exists()
        assert AuditLog.objects.filter(action='payment.amount_mismatch').exists()

    def test_wrong_currency_is_left_for_a_person(self, booking):
        ref, _ = start(booking)
        txn = Transaction.objects.get(provider_ref=ref)
        with mock.patch.object(paystack, 'verify', return_value=paid(txn, currency='NGN')):
            checkout.settle(ref)
        txn.refresh_from_db()
        assert txn.status == TransactionStatus.PENDING

    def test_failure_is_recorded_with_the_reason(self, booking):
        ref, _ = start(booking)
        with mock.patch.object(paystack, 'verify',
                               return_value={'status': 'failed', 'gateway_response': 'Declined'}):
            checkout.settle(ref)
        txn = Transaction.objects.get(provider_ref=ref)
        assert txn.status == TransactionStatus.FAILED and txn.failure_reason == 'Declined'
        booking.refresh_from_db()
        assert booking.status == BookingStatus.PENDING  # they can try again

    @pytest.mark.parametrize('state', ['abandoned', 'ongoing', 'pending', 'something_new'])
    def test_unfinished_changes_nothing(self, booking, state):
        ref, _ = start(booking)
        with mock.patch.object(paystack, 'verify', return_value={'status': state}):
            checkout.settle(ref)
        assert Transaction.objects.get(provider_ref=ref).status == TransactionStatus.PENDING

    def test_success_after_we_marked_it_failed_is_still_recorded(self, booking):
        # Out of order: the sweep closed an "abandoned" checkout, then the
        # customer finished paying in the tab they left open.
        ref, _ = start(booking)
        txn = Transaction.objects.get(provider_ref=ref)
        from apps.payments.ledger import mark_failed
        mark_failed(txn, 'Checkout was not completed')
        with mock.patch.object(paystack, 'verify', return_value=paid(txn)):
            checkout.settle(ref)
        txn.refresh_from_db()
        assert txn.status == TransactionStatus.SUCCEEDED
        assert AuditLog.objects.get(action='payment.succeeded').metadata['after_marked_failed']

    def test_paid_after_cancelling_is_recorded_and_flagged(self, booking):
        ref, _ = start(booking)
        txn = Transaction.objects.get(provider_ref=ref)
        StudioBooking.objects.filter(pk=booking.pk).update(status=BookingStatus.CANCELLED)
        with mock.patch.object(paystack, 'verify', return_value=paid(txn)):
            checkout.settle(ref)
        booking.refresh_from_db()
        assert booking.status == BookingStatus.CANCELLED
        assert Transaction.objects.get(pk=txn.pk).status == TransactionStatus.SUCCEEDED
        assert AuditLog.objects.filter(action='payment.received_on_cancelled').exists()

    def test_reversed_is_flagged(self, booking):
        ref, _ = start(booking)
        with mock.patch.object(paystack, 'verify', return_value={'status': 'reversed'}):
            checkout.settle(ref)
        assert AuditLog.objects.filter(action='payment.reversed').exists()

    def test_unknown_reference_is_ignored(self):
        with mock.patch.object(paystack, 'verify') as verify:
            assert checkout.settle('not-ours') is None
        verify.assert_not_called()

    def test_an_already_settled_payment_does_not_call_paystack_again(self, booking):
        ref, _ = start(booking)
        txn = Transaction.objects.get(provider_ref=ref)
        with mock.patch.object(paystack, 'verify', return_value=paid(txn)):
            checkout.settle(ref)
        with mock.patch.object(paystack, 'verify') as again:
            checkout.settle(ref)
        again.assert_not_called()


# ---------------------------------------------------------------------------
# Webhook (Task 9)
# ---------------------------------------------------------------------------

class TestWebhook:
    def test_replayed_webhook_holds_once_and_confirms_once(self, booking):
        # The test the plan says is not optional.
        ref, _ = start(booking)
        txn = Transaction.objects.get(provider_ref=ref)
        with mock.patch.object(paystack, 'verify', return_value=paid(txn)):
            assert signed_post(charge_success(ref)).status_code == 200
            assert signed_post(charge_success(ref)).status_code == 200
        assert LedgerEntry.objects.filter(transaction=txn, entry_type=EntryType.HOLD).count() == 1
        assert AuditLog.objects.filter(action='booking.confirmed').count() == 1
        assert AuditLog.objects.filter(action='payment.succeeded').count() == 1

    def test_webhook_and_return_page_together_hold_once(self, booking):
        ref, _ = start(booking)
        txn = Transaction.objects.get(provider_ref=ref)
        with mock.patch.object(paystack, 'verify', return_value=paid(txn)):
            signed_post(charge_success(ref))
            res = client_for(booking.customer).post('/api/v1/payments/verify/',
                                                    {'reference': ref}, format='json')
        assert res.data['status'] == 'succeeded'
        assert LedgerEntry.objects.filter(transaction=txn).count() == 1

    def test_unsigned_is_refused_before_anything_happens(self, booking):
        ref, _ = start(booking)
        with mock.patch.object(paystack, 'verify') as verify:
            res = APIClient().post(WEBHOOK, data=json.dumps(charge_success(ref)),
                                   content_type='application/json')
        assert res.status_code == 401
        verify.assert_not_called()

    def test_forged_signature_is_refused(self, booking):
        ref, _ = start(booking)
        raw = json.dumps(charge_success(ref)).encode()
        sig = hmac.new(b'sk_test_attacker', raw, hashlib.sha512).hexdigest()
        res = APIClient().post(WEBHOOK, data=raw, content_type='application/json',
                               HTTP_X_PAYSTACK_SIGNATURE=sig)
        assert res.status_code == 401
        assert Transaction.objects.get(provider_ref=ref).status == TransactionStatus.PENDING

    def test_a_signed_body_is_still_verified_with_paystack(self, booking):
        # Even a genuine event is a prompt to ask, not the answer.
        ref, _ = start(booking)
        with mock.patch.object(paystack, 'verify', return_value={'status': 'abandoned'}):
            signed_post(charge_success(ref))
        assert Transaction.objects.get(provider_ref=ref).status == TransactionStatus.PENDING

    def test_other_events_are_acknowledged_and_ignored(self):
        with mock.patch.object(paystack, 'verify') as verify:
            res = signed_post({'event': 'transfer.success', 'data': {'reference': 'x'}})
        assert res.status_code == 200
        verify.assert_not_called()

    def test_paystack_unreachable_asks_for_a_retry(self, booking):
        ref, _ = start(booking)
        with mock.patch.object(paystack, 'verify', side_effect=paystack.PaystackError('down')):
            res = signed_post(charge_success(ref))
        assert res.status_code == 503  # Paystack retries non-200s


# ---------------------------------------------------------------------------
# Return page
# ---------------------------------------------------------------------------

class TestVerifyEndpoint:
    def test_someone_elses_reference_is_a_404(self, booking, world):
        ref, _ = start(booking)
        res = client_for(world['other']).post('/api/v1/payments/verify/', {'reference': ref},
                                              format='json')
        assert res.status_code == 404

    def test_paystack_down_reads_as_still_checking(self, booking):
        ref, _ = start(booking)
        with mock.patch.object(paystack, 'verify', side_effect=paystack.PaystackError('down')):
            res = client_for(booking.customer).post('/api/v1/payments/verify/',
                                                    {'reference': ref}, format='json')
        assert res.status_code == 200
        assert res.data['status'] == 'pending' and res.data['checked'] is False

    def test_reports_the_booking(self, booking):
        ref, _ = start(booking)
        txn = Transaction.objects.get(provider_ref=ref)
        with mock.patch.object(paystack, 'verify', return_value=paid(txn)):
            res = client_for(booking.customer).post('/api/v1/payments/verify/',
                                                    {'reference': ref}, format='json')
        assert res.data['booking'] == {'id': str(booking.pk), 'reference': booking.reference,
                                       'status': 'confirmed'}


# ---------------------------------------------------------------------------
# Reconcile sweep
# ---------------------------------------------------------------------------

def _age(ref, minutes):
    Transaction.objects.filter(provider_ref=ref).update(
        created_at=timezone.now() - timedelta(minutes=minutes))


class TestReconcile:
    def test_settles_a_payment_whose_webhook_was_lost(self, booking):
        ref, _ = start(booking)
        _age(ref, 15)
        txn = Transaction.objects.get(provider_ref=ref)
        with mock.patch.object(paystack, 'verify', return_value=paid(txn)):
            counts = checkout.reconcile_pending()
        assert counts['succeeded'] == 1
        booking.refresh_from_db()
        assert booking.status == BookingStatus.CONFIRMED

    def test_leaves_a_fresh_checkout_alone(self, booking):
        start(booking)
        with mock.patch.object(paystack, 'verify') as verify:
            checkout.reconcile_pending()
        verify.assert_not_called()

    def test_closes_an_abandoned_checkout_after_two_hours(self, booking):
        recent, _ = start(booking)
        old, _ = start(booking)
        _age(recent, 30)
        _age(old, 180)
        with mock.patch.object(paystack, 'verify', return_value={'status': 'abandoned'}):
            counts = checkout.reconcile_pending()
        assert counts['abandoned'] == 1
        assert Transaction.objects.get(provider_ref=old).status == TransactionStatus.FAILED
        assert Transaction.objects.get(provider_ref=recent).status == TransactionStatus.PENDING

    def test_one_error_does_not_stop_the_sweep(self, world):
        a, _ = start(make_booking(world))
        b, _ = start(make_booking(world))
        _age(a, 20)
        _age(b, 20)
        tb = Transaction.objects.get(provider_ref=b)

        def verify(ref):
            if ref == a:
                raise paystack.PaystackError('down')
            return paid(tb)
        with mock.patch.object(paystack, 'verify', side_effect=verify):
            counts = checkout.reconcile_pending()
        assert counts == {'checked': 2, 'succeeded': 1, 'failed': 0, 'abandoned': 0, 'errors': 1}

    def test_does_nothing_without_a_key(self, booking, settings):
        ref, _ = start(booking)
        _age(ref, 20)
        settings.PAYSTACK_SECRET_KEY = ''
        assert checkout.reconcile_pending()['checked'] == 0


# ---------------------------------------------------------------------------
# What the customer API tells the frontend
# ---------------------------------------------------------------------------

class TestCanPayOnline:
    def _get(self, booking):
        return client_for(booking.customer).get(f'/api/v1/studio/bookings/{booking.pk}/').data

    def test_pending_and_owing(self, booking):
        data = self._get(booking)
        assert data['can_pay_online'] is True
        assert data['outstanding_pesewas'] == 40000 and data['outstanding_display'] == 'GHS 400.00'

    def test_not_without_a_key(self, booking, settings):
        settings.PAYSTACK_SECRET_KEY = ''
        assert self._get(booking)['can_pay_online'] is False

    def test_not_once_paid(self, booking):
        ref, _ = start(booking)
        txn = Transaction.objects.get(provider_ref=ref)
        with mock.patch.object(paystack, 'verify', return_value=paid(txn)):
            checkout.settle(ref)
        data = self._get(booking)
        assert data['can_pay_online'] is False and data['outstanding_pesewas'] == 0
