from unittest.mock import Mock

import pytest
import requests
from django.test import override_settings
from rest_framework.exceptions import ValidationError

from apps.payments.hubtel import (
    HubtelClient,
    ghs_string_to_pesewas,
    pesewas_to_ghs_string,
)


@pytest.mark.parametrize('pesewas, expected', [
    (1, '0.01'),
    (1250, '12.50'),
    (125_000, '1250.00'),
])
def test_pesewas_to_ghs_string_is_exact(pesewas, expected):
    assert pesewas_to_ghs_string(pesewas) == expected


@pytest.mark.parametrize('value', [True, 12.5, '1250', 0, -1])
def test_pesewas_to_ghs_string_rejects_invalid_values(value):
    with pytest.raises((TypeError, ValueError)):
        pesewas_to_ghs_string(value)


@pytest.mark.parametrize('amount, expected', [
    ('0.01', 1),
    ('12.50', 1250),
    ('1250', 125_000),
])
def test_ghs_string_to_pesewas_is_exact(amount, expected):
    assert ghs_string_to_pesewas(amount) == expected


@pytest.mark.parametrize('amount', ['12.345', 'not-money', 'NaN', 'Infinity'])
def test_ghs_string_to_pesewas_rejects_ambiguous_values(amount):
    with pytest.raises(ValueError):
        ghs_string_to_pesewas(amount)


@pytest.mark.parametrize('phone, expected', [
    ('024 123 4567', '233241234567'),
    ('+233-24-123-4567', '233241234567'),
    ('241234567', '233241234567'),
])
def test_phone_normalisation(phone, expected):
    assert HubtelClient.format_phone(phone) == expected
    assert HubtelClient.to_local_format(phone) == '0241234567'


@pytest.mark.parametrize('phone', ['024ABC4567', '123', '+442071234567'])
def test_phone_normalisation_rejects_invalid_numbers(phone):
    with pytest.raises(ValueError):
        HubtelClient.format_phone(phone)


@pytest.mark.parametrize('phone, expected', [
    ('0241234567', 'mtn-gh'),
    ('0201234567', 'vodafone-gh'),
    ('0271234567', 'tigo-gh'),
])
def test_channel_detection(phone, expected):
    assert HubtelClient.detect_channel(phone) == expected


@override_settings(
    HUBTEL_POS_SALES_ID='merchant-1',
    HUBTEL_MERCHANT_ID='',
    HUBTEL_API_KEY='key',
    HUBTEL_API_SECRET='secret',
    HUBTEL_CALLBACK_URL='https://example.com/callback',
)
def test_initiate_payment_sends_the_provider_contract(monkeypatch):
    response = Mock()
    response.json.return_value = {'responseCode': '0000'}
    post = Mock(return_value=response)
    monkeypatch.setattr('apps.payments.hubtel.requests.post', post)

    result = HubtelClient.initiate_payment(
        reference='booking-123',
        amount_pesewas=1250,
        phone='0241234567',
        customer_name='Test Customer',
        description='x' * 120,
    )

    assert result == {'responseCode': '0000'}
    post.assert_called_once()
    _, kwargs = post.call_args
    assert kwargs['json']['amount'] == '12.50'
    assert kwargs['json']['customerMsisdn'] == '233241234567'
    assert len(kwargs['json']['description']) == 100
    assert kwargs['timeout'] == 30


@override_settings(
    HUBTEL_POS_SALES_ID='merchant-1',
    HUBTEL_MERCHANT_ID='',
    HUBTEL_API_KEY='key',
    HUBTEL_API_SECRET='secret',
    HUBTEL_CALLBACK_URL='https://example.com/callback',
)
@pytest.mark.parametrize('failure', [
    requests.ConnectionError('provider unavailable'),
    requests.Timeout('provider timed out'),
])
def test_initiate_network_failure_returns_safe_guidance(monkeypatch, failure):
    monkeypatch.setattr('apps.payments.hubtel.requests.post', Mock(side_effect=failure))

    with pytest.raises(ValidationError, match='do not pay twice'):
        HubtelClient.initiate_payment(
            'booking-123', 1250, '0241234567', 'Test Customer', 'Booking',
        )


@override_settings(
    HUBTEL_POS_SALES_ID='merchant-1',
    HUBTEL_MERCHANT_ID='',
    HUBTEL_API_KEY='key',
    HUBTEL_API_SECRET='secret',
    HUBTEL_CALLBACK_URL='https://example.com/callback',
)
def test_initiate_provider_rejection_returns_safe_error(monkeypatch):
    response = Mock(status_code=400, text='provider detail')
    response.raise_for_status.side_effect = requests.HTTPError(response=response)
    monkeypatch.setattr('apps.payments.hubtel.requests.post', Mock(return_value=response))

    with pytest.raises(ValidationError, match='provider rejected'):
        HubtelClient.initiate_payment(
            'booking-123', 1250, '0241234567', 'Test Customer', 'Booking',
        )


@override_settings(
    HUBTEL_POS_SALES_ID='merchant-1',
    HUBTEL_MERCHANT_ID='',
    HUBTEL_API_KEY='key',
    HUBTEL_API_SECRET='secret',
)
def test_status_lookup_passes_reference_as_query_parameter(monkeypatch):
    response = Mock()
    response.json.return_value = {'status': 'Paid'}
    get = Mock(return_value=response)
    monkeypatch.setattr('apps.payments.hubtel.requests.get', get)

    result = HubtelClient.check_transaction_status('booking/123?retry=true')

    assert result == {'status': 'Paid'}
    get.assert_called_once()
    _, kwargs = get.call_args
    assert kwargs['params'] == {'clientReference': 'booking/123?retry=true'}
    assert kwargs['timeout'] == 15


@override_settings(HUBTEL_POS_SALES_ID='', HUBTEL_MERCHANT_ID='')
@pytest.mark.parametrize('operation', [
    lambda: HubtelClient.initiate_payment('ref', 100, '0241234567', 'Name', 'Description'),
    lambda: HubtelClient.check_transaction_status('ref'),
])
def test_provider_calls_fail_fast_when_unconfigured(operation):
    with pytest.raises(ValidationError, match='not configured'):
        operation()


@override_settings(
    HUBTEL_POS_SALES_ID='merchant-1',
    HUBTEL_MERCHANT_ID='',
    HUBTEL_API_KEY='key',
    HUBTEL_API_SECRET='secret',
)
def test_network_failure_returns_a_safe_payment_error(monkeypatch):
    monkeypatch.setattr(
        'apps.payments.hubtel.requests.get',
        Mock(side_effect=requests.ConnectionError('provider unavailable')),
    )

    with pytest.raises(ValidationError, match='Could not reach'):
        HubtelClient.check_transaction_status('ref')


@override_settings(
    HUBTEL_POS_SALES_ID='merchant-1',
    HUBTEL_MERCHANT_ID='',
    HUBTEL_API_KEY='key',
    HUBTEL_API_SECRET='secret',
)
def test_status_provider_rejection_returns_safe_error(monkeypatch):
    response = Mock(status_code=404, text='provider detail')
    response.raise_for_status.side_effect = requests.HTTPError(response=response)
    monkeypatch.setattr('apps.payments.hubtel.requests.get', Mock(return_value=response))

    with pytest.raises(ValidationError, match='retrieve payment status'):
        HubtelClient.check_transaction_status('ref')
