"""
Paystack client — card and Mobile Money for ONA Records (ADR-018).

Redirect flow: the server initialises a transaction, the customer pays on
Paystack's hosted checkout (card, MTN MoMo, Telecel Cash, AirtelTigo Money —
whatever ONA enables on its dashboard), and Paystack sends them back to the
callback URL. No card number or MoMo PIN ever reaches this system.

Three things this module does, and nothing else:

    initialize(reference, amount_pesewas, email, ...)  -> {authorization_url, ...}
    verify(reference)                                  -> Paystack's `data` object
    signature_is_valid(raw_body, header)               -> bool

AMOUNTS: Paystack takes and returns amounts in the currency's subunit — for
GHS, pesewas — as integers. That is the representation this codebase already
uses everywhere, so there is no conversion at this boundary at all (the
Hubtel client needed a Decimal string; this one does not).

TRUST: a webhook proves nothing until its signature checks out, and even
then the money decision is made on `verify`, which asks Paystack directly.
The callback URL being visited proves nothing either — anyone can type it.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

API = 'https://api.paystack.co'


class PaystackError(Exception):
    """Paystack refused, or could not be reached. The message is safe to show."""


def is_configured() -> bool:
    return bool(getattr(settings, 'PAYSTACK_SECRET_KEY', ''))


def is_test_key(key: str | None = None) -> bool:
    key = settings.PAYSTACK_SECRET_KEY if key is None else key
    return key.startswith('sk_test_')


def _headers() -> dict:
    return {
        'Authorization': f'Bearer {settings.PAYSTACK_SECRET_KEY}',
        'Content-Type': 'application/json',
    }


def initialize(reference: str, amount_pesewas: int, email: str, callback_url: str,
               metadata: dict | None = None) -> dict:
    """
    Start a hosted checkout. Returns Paystack's `data`: authorization_url,
    access_code, reference.

    `reference` is ours and unique per attempt — it is what the webhook and
    the return page hand back, and the key a replay is matched on.
    """
    if type(amount_pesewas) is not int or amount_pesewas <= 0:
        raise TypeError('Amount must be a positive int of pesewas, got %r.' % (amount_pesewas,))
    if not is_configured():
        raise PaystackError('Online payment is not available yet. ONA will be in touch '
                            'about paying for this booking.')

    payload = {
        'email': email,
        'amount': amount_pesewas,
        'currency': 'GHS',
        'reference': reference,
        'callback_url': callback_url,
        # Ids only: metadata is visible on Paystack's dashboard.
        'metadata': metadata or {},
    }
    logger.info('Paystack initialize: ref=%s amount_pesewas=%d', reference, amount_pesewas)
    try:
        resp = requests.post(f'{API}/transaction/initialize', json=payload,
                             headers=_headers(), timeout=20)
    except requests.RequestException as exc:
        logger.error('Paystack initialize network error: ref=%s err=%s', reference, exc)
        raise PaystackError('Could not reach the payment provider. Please try again.')
    body = _json(resp)
    if resp.status_code >= 400 or not body.get('status'):
        logger.error('Paystack initialize refused: ref=%s status=%s message=%s',
                     reference, resp.status_code, str(body.get('message'))[:200])
        raise PaystackError('The payment provider could not start this payment. '
                            'Please try again in a moment.')
    return body['data']


def verify(reference: str) -> dict:
    """
    Ask Paystack what actually happened. The source of truth for money:
    `data.status` ('success', 'failed', 'abandoned', 'ongoing', 'pending',
    'reversed', ...) and `data.amount` in pesewas.

    Note `body.status` is whether the API CALL worked, not the payment.
    """
    try:
        resp = requests.get(f'{API}/transaction/verify/{requests.utils.quote(reference, safe="")}',
                            headers=_headers(), timeout=15)
    except requests.RequestException as exc:
        logger.error('Paystack verify network error: ref=%s err=%s', reference, exc)
        raise PaystackError('Could not reach the payment provider.')
    body = _json(resp)
    if resp.status_code >= 400 or not body.get('status') or not isinstance(body.get('data'), dict):
        logger.error('Paystack verify refused: ref=%s status=%s message=%s',
                     reference, resp.status_code, str(body.get('message'))[:200])
        raise PaystackError('Could not confirm this payment with the provider.')
    return body['data']


def signature_is_valid(raw_body: bytes, header: str | None) -> bool:
    """
    `x-paystack-signature` is HMAC-SHA512 of the exact request bytes, keyed
    with the secret key. Computed over the raw body — re-serialising parsed
    JSON would change whitespace and key order and never match.
    """
    if not header or not is_configured():
        return False
    expected = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, header.strip())


def _json(resp) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
