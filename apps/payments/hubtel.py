"""
Hubtel client — mobile money for ONA Records.

Adapted from the EaglesnEaglets implementation, which is already proven
against Hubtel's Receive Money v2 API. The network detection and phone
formatting are kept as-is; they encode real knowledge of Ghanaian numbers.

ONE DELIBERATE DIFFERENCE FROM THE REFERENCE

The reference passes amounts as float. This client takes integer pesewas and
converts to the decimal string Hubtel expects only at the boundary.

Money is stored, compared and summed as integers throughout this codebase.
Floats cannot represent 0.1 exactly, so repeated float arithmetic on a ledger
drifts — and this ledger decides what a professional is paid. The conversion
happens once, here, at the edge.

No card data ever passes through this system: Hubtel sends a prompt to the
customer's phone and the customer authorises it there.
"""

import base64
import logging
import re
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from rest_framework.exceptions import ValidationError

logger = logging.getLogger(__name__)


def pesewas_to_ghs_string(pesewas: int) -> str:
    """
    Convert integer pesewas to the decimal string Hubtel expects.

    1250 -> "12.50". Decimal, not float: this is the one place the value
    crosses into a decimal representation, and it must be exact.
    """
    if type(pesewas) is not int:
        raise TypeError(
            'Amount must be an integer number of pesewas, got %r (%s). '
            'Storing money as float or Decimal is a bug in this codebase.'
            % (pesewas, type(pesewas).__name__)
        )
    if pesewas <= 0:
        raise ValueError('Amount must be positive, got %d pesewas.' % pesewas)
    amount = Decimal(pesewas) / Decimal(settings.CURRENCY_MINOR_UNITS)
    return format(amount, '.2f')


def ghs_string_to_pesewas(amount: str) -> int:
    """
    Convert a Hubtel amount string back to integer pesewas.

    Used when reconciling a callback against what we expected to be charged.
    """
    try:
        decimal_amount = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('Hubtel amount must be a valid decimal string.') from exc

    if not decimal_amount.is_finite():
        raise ValueError('Hubtel amount must be finite.')

    pesewas = decimal_amount * settings.CURRENCY_MINOR_UNITS
    if pesewas != pesewas.to_integral_value():
        raise ValueError('Hubtel amount contains a fractional pesewa.')
    return int(pesewas)


class HubtelClient:
    """Thin wrapper around Hubtel's Receive Money API."""

    BASE_URL = 'https://rmp.hubtel.com/merchantaccount/merchants/{account}/receive/mobilemoney'
    STATUS_URL = 'https://api-txnstatus.hubtel.com/transactions/{account}/status'

    # Ghana network prefixes. Note 026 and 056 are shared between networks
    # after portability, so detection is a best guess — Hubtel still routes
    # correctly if the channel is wrong, it is only slower.
    _MTN_PREFIXES = ('024', '054', '055', '059', '025')
    _VODAFONE_PREFIXES = ('020', '050')
    _AIRTELTIGO_PREFIXES = ('027', '057', '026', '056')

    @staticmethod
    def _auth_header() -> dict:
        credentials = base64.b64encode(
            f'{settings.HUBTEL_API_KEY}:{settings.HUBTEL_API_SECRET}'.encode()
        ).decode()
        return {
            'Authorization': f'Basic {credentials}',
            'Content-Type': 'application/json',
        }

    @staticmethod
    def _account_id() -> str:
        account = settings.HUBTEL_POS_SALES_ID or settings.HUBTEL_MERCHANT_ID
        if not account:
            raise ValidationError(
                {'payment': 'Payments are not configured. Contact ONA.'}
            )
        return account

    @classmethod
    def detect_channel(cls, phone: str) -> str:
        """Guess the network from a Ghana number. Returns a Hubtel channel."""
        local = cls.to_local_format(phone)

        if local.startswith(cls._MTN_PREFIXES):
            return 'mtn-gh'
        if local.startswith(cls._VODAFONE_PREFIXES):
            return 'vodafone-gh'
        return 'tigo-gh'

    @staticmethod
    def format_phone(phone: str) -> str:
        """Normalise a Ghana number to E.164 without the plus: 233XXXXXXXXX."""
        digits = re.sub(r'[\s\-()]', '', phone)
        if digits.startswith('+'):
            digits = digits[1:]
        if not digits.isdigit():
            raise ValueError('Phone number may contain digits and formatting characters only.')
        if len(digits) == 10 and digits.startswith('0'):
            return '233' + digits[1:]
        if len(digits) == 9:
            return '233' + digits
        if len(digits) == 12 and digits.startswith('233'):
            return digits
        raise ValueError('Phone number must be a valid Ghanaian mobile number.')

    @staticmethod
    def to_local_format(phone: str) -> str:
        """Convert 233XXXXXXXXX back to 0XXXXXXXXX."""
        international = HubtelClient.format_phone(phone)
        return '0' + international[3:]

    @classmethod
    def initiate_payment(
        cls,
        reference: str,
        amount_pesewas: int,
        phone: str,
        customer_name: str,
        description: str,
        callback_url: str = None,
        channel: str = None,
    ) -> dict:
        """
        Ask Hubtel to prompt the customer's phone for authorisation.

        `reference` is our own idempotency key and must be unique per attempt;
        it is what ties the callback back to a booking or project.
        Returns Hubtel's raw response.
        """
        account = cls._account_id()

        payload = {
            'customerName': customer_name,
            'customerMsisdn': cls.format_phone(phone),
            'channel': channel or cls.detect_channel(phone),
            'amount': pesewas_to_ghs_string(amount_pesewas),
            'primaryCallbackUrl': callback_url or settings.HUBTEL_CALLBACK_URL,
            'clientReference': reference,
            'description': description[:100],
            'posSalesId': account,
        }

        # Never log the payload: it contains the customer's phone number.
        logger.info(
            'Hubtel initiate: ref=%s channel=%s amount_pesewas=%d',
            reference, payload['channel'], amount_pesewas,
        )

        try:
            resp = requests.post(
                cls.BASE_URL.format(account=account),
                json=payload,
                headers=cls._auth_header(),
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            logger.error(
                'Hubtel initiate failed: ref=%s status=%s body=%s',
                reference, exc.response.status_code, exc.response.text[:500],
            )
            raise ValidationError({
                'payment': 'The payment provider rejected the request. '
                           'Check the phone number and try again.'
            })
        except requests.RequestException as exc:
            # The customer may still have been prompted. Never mark a payment
            # failed on a network error alone — reconcile via status check.
            logger.error('Hubtel network error: ref=%s err=%s', reference, exc)
            raise ValidationError({
                'payment': 'Could not reach the payment provider. '
                           'If your phone was prompted, do not pay twice — '
                           'check your bookings in a moment.'
            })

    @classmethod
    def check_transaction_status(cls, client_reference: str) -> dict:
        """
        Ask Hubtel what actually happened to a transaction.

        This is the source of truth, not the callback: callbacks can be lost,
        duplicated or delayed, so anything left pending must be reconciled
        by polling here.
        """
        account = cls._account_id()
        url = cls.STATUS_URL.format(account=account)
        try:
            resp = requests.get(
                url,
                params={'clientReference': client_reference},
                headers=cls._auth_header(),
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            logger.error(
                'Hubtel status failed: ref=%s status=%s body=%s',
                client_reference, exc.response.status_code, exc.response.text[:300],
            )
            raise ValidationError({'payment': 'Could not retrieve payment status.'})
        except requests.RequestException as exc:
            logger.error('Hubtel status network error: ref=%s err=%s', client_reference, exc)
            raise ValidationError({'payment': 'Could not reach the payment provider.'})
