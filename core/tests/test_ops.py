"""Task 20 operations tools: deep health, ops_check, reconcile_payments."""

from io import StringIO
from unittest import mock

import pytest
from django.core.management import CommandError, call_command
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


def test_liveness_stays_shallow():
    res = APIClient().get('/api/v1/health/')
    assert res.status_code == 200 and 'checks' not in res.json()


def test_deep_health_touches_database_and_cache():
    res = APIClient().get('/api/v1/health/?deep=1')
    assert res.status_code == 200
    assert res.json()['checks'] == {'database': 'ok', 'cache': 'ok'}


def test_deep_health_is_503_when_the_cache_is_down():
    with mock.patch('django.core.cache.cache.set', side_effect=ConnectionError('down')):
        res = APIClient().get('/api/v1/health/?deep=1')
    assert res.status_code == 503 and res.json()['checks']['cache'] == 'down'


def test_ops_check_passes_locally_and_warns_about_what_is_missing(settings):
    settings.CELERY_BROKER_URL = ''
    settings.SENTRY_DSN = ''
    out = StringIO()
    call_command('ops_check', stdout=out)
    text = out.getvalue()
    assert 'database' in text and 'no broker configured' in text and 'SENTRY_DSN not set' in text


def test_ops_check_fails_when_the_database_is_down():
    with mock.patch('django.db.backends.utils.CursorWrapper.execute', side_effect=Exception('gone')):
        with pytest.raises(CommandError, match='database'):
            call_command('ops_check', stdout=StringIO())


def test_reconcile_payments_needs_a_key(settings):
    settings.PAYSTACK_SECRET_KEY = ''
    with pytest.raises(CommandError, match='PAYSTACK_SECRET_KEY'):
        call_command('reconcile_payments', stdout=StringIO())


def test_reconcile_payments_unknown_reference(settings):
    settings.PAYSTACK_SECRET_KEY = 'sk_test_x'
    with pytest.raises(CommandError, match='No Paystack payment'):
        call_command('reconcile_payments', '--reference', 'nope', stdout=StringIO())


def test_reconcile_payments_sweeps(settings):
    settings.PAYSTACK_SECRET_KEY = 'sk_test_x'
    out = StringIO()
    call_command('reconcile_payments', '--release', stdout=out)
    assert 'Pending payments: checked 0' in out.getvalue()
    assert 'Unpaid bookings released: 0' in out.getvalue()
