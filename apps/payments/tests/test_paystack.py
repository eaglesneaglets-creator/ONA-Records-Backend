"""
Paystack client (ADR-018), mocked at the HTTP boundary.

What has to be true:
  * amounts go out as integer pesewas, in GHS, with our reference;
  * a webhook signature is HMAC-SHA512 over the RAW body, and anything else
    — a re-serialised body, another key, no header — is refused;
  * a refusal or a network error surfaces as PaystackError with a message
    safe to show a customer, never a stack trace or Paystack's own text.
"""

import hashlib
import hmac
import json
from unittest import mock

import pytest
import requests

from apps.payments import paystack

KEY = 'sk_test_abc123'


@pytest.fixture(autouse=True)
def key(settings):
    settings.PAYSTACK_SECRET_KEY = KEY


def _resp(status=200, body=None):
    r = mock.Mock(status_code=status)
    r.json.return_value = body if body is not None else {}
    return r


class TestInitialize:
    def test_sends_integer_pesewas_in_ghs_with_our_reference(self):
        ok = _resp(body={'status': True, 'data': {'authorization_url': 'https://checkout.paystack.com/x',
                                                  'access_code': 'x', 'reference': 'R1'}})
        with mock.patch.object(paystack.requests, 'post', return_value=ok) as post:
            data = paystack.initialize('R1', 12550, 'c@x.test', 'https://ona.test/return',
                                       metadata={'booking_reference': 'ONA-1'})
        sent = post.call_args.kwargs['json']
        assert sent['amount'] == 12550 and type(sent['amount']) is int
        assert sent['currency'] == 'GHS' and sent['reference'] == 'R1'
        assert sent['callback_url'] == 'https://ona.test/return'
        assert post.call_args.kwargs['headers']['Authorization'] == f'Bearer {KEY}'
        assert data['authorization_url'].startswith('https://checkout.paystack.com/')

    @pytest.mark.parametrize('amount', [0, -1, 12.5, '100', None])
    def test_refuses_anything_but_a_positive_int(self, amount):
        with pytest.raises(TypeError):
            paystack.initialize('R', amount, 'c@x.test', 'u')

    def test_not_configured_is_a_clear_refusal(self, settings):
        settings.PAYSTACK_SECRET_KEY = ''
        with pytest.raises(paystack.PaystackError, match='not available yet'):
            paystack.initialize('R', 100, 'c@x.test', 'u')

    def test_a_refusal_never_leaks_paystacks_message(self):
        bad = _resp(400, {'status': False, 'message': 'Duplicate Transaction Reference'})
        with mock.patch.object(paystack.requests, 'post', return_value=bad):
            with pytest.raises(paystack.PaystackError) as exc:
                paystack.initialize('R', 100, 'c@x.test', 'u')
        assert 'Duplicate' not in str(exc.value)

    def test_network_error(self):
        with mock.patch.object(paystack.requests, 'post', side_effect=requests.ConnectionError):
            with pytest.raises(paystack.PaystackError, match='Could not reach'):
                paystack.initialize('R', 100, 'c@x.test', 'u')


class TestVerify:
    def test_returns_the_data_object(self):
        body = {'status': True, 'data': {'status': 'success', 'amount': 100, 'currency': 'GHS'}}
        with mock.patch.object(paystack.requests, 'get', return_value=_resp(body=body)) as get:
            data = paystack.verify('ONA-1-AB')
        assert data['status'] == 'success'
        assert get.call_args.args[0].endswith('/transaction/verify/ONA-1-AB')

    def test_the_reference_cannot_change_the_path(self):
        body = {'status': True, 'data': {}}
        with mock.patch.object(paystack.requests, 'get', return_value=_resp(body=body)) as get:
            paystack.verify('../../customer')
        assert '/transaction/verify/..%2F..%2Fcustomer' in get.call_args.args[0]

    def test_api_call_failed_is_not_a_payment_status(self):
        # body.status False means the CALL failed; it says nothing about money.
        with mock.patch.object(paystack.requests, 'get',
                               return_value=_resp(400, {'status': False, 'message': 'not found'})):
            with pytest.raises(paystack.PaystackError):
                paystack.verify('nope')

    def test_non_json_reply(self):
        r = _resp(502)
        r.json.side_effect = ValueError
        with mock.patch.object(paystack.requests, 'get', return_value=r):
            with pytest.raises(paystack.PaystackError):
                paystack.verify('x')


class TestSignature:
    BODY = json.dumps({'event': 'charge.success', 'data': {'reference': 'R1'}}).encode()

    def sign(self, body, key=KEY):
        return hmac.new(key.encode(), body, hashlib.sha512).hexdigest()

    def test_valid(self):
        assert paystack.signature_is_valid(self.BODY, self.sign(self.BODY))

    def test_signed_with_another_key(self):
        assert not paystack.signature_is_valid(self.BODY, self.sign(self.BODY, 'sk_test_other'))

    def test_body_changed_by_one_byte(self):
        assert not paystack.signature_is_valid(self.BODY + b' ', self.sign(self.BODY))

    def test_reserialised_json_does_not_match(self):
        # Why the view must read request.body, not re-dump request.data.
        redumped = json.dumps(json.loads(self.BODY), separators=(',', ':')).encode()
        assert not paystack.signature_is_valid(redumped, self.sign(self.BODY))

    @pytest.mark.parametrize('header', [None, ''])
    def test_missing_header(self, header):
        assert not paystack.signature_is_valid(self.BODY, header)

    def test_unconfigured_accepts_nothing(self, settings):
        sig = self.sign(self.BODY)
        settings.PAYSTACK_SECRET_KEY = ''
        assert not paystack.signature_is_valid(self.BODY, sig)


def test_is_test_key():
    assert paystack.is_test_key('sk_test_1') and not paystack.is_test_key('sk_live_1')
