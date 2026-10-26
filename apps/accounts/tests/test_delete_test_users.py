"""
The E2E test-data sweep: `manage.py delete_test_users --prefix e2e-`.

Money records are evidence and the ledger is append-only, so an account
that ever paid is never swept silently: by default the command refuses;
with --skip-with-payments it keeps those accounts and sweeps the rest.
"""

from datetime import timedelta

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.marketplace.models import Job
from apps.payments import ledger
from apps.studio.models import Engineer, Room, Service, StudioBooking

pytestmark = pytest.mark.django_db


@pytest.fixture
def data(django_user_model):
    U = django_user_model.objects
    room = Room.objects.create(name='A', hourly_rate_pesewas=100)
    eng = Engineer.objects.create(name='E')
    svc = Service.objects.create(name='S', base_price_pesewas=1, duration_minutes=60)

    def booking(user, days):
        start = timezone.now() + timedelta(days=days)
        return StudioBooking.objects.create(customer=user, room=room, engineer=eng, service=svc,
                                            start_at=start, end_at=start + timedelta(hours=1),
                                            total_pesewas=100)

    paid_user = U.create_user(email='e2e-paid@x.test', password='p-Ass-1', role='customer')
    plain_user = U.create_user(email='e2e-plain@x.test', password='p-Ass-1', role='customer')
    keep_user = U.create_user(email='real@x.test', password='p-Ass-1', role='customer')
    ledger.mark_succeeded(ledger.open_transaction(booking(paid_user, 3)), provider_ref='HUB-T')
    booking(plain_user, 4)
    Job.objects.create(customer=plain_user, title='Test job')
    return {'paid': paid_user, 'plain': plain_user, 'keep': keep_user}


def test_refuses_by_default_when_any_account_has_paid(data, django_user_model):
    with pytest.raises(CommandError, match='payment records'):
        call_command('delete_test_users', prefix='e2e-')
    assert django_user_model.objects.filter(email__startswith='e2e-').count() == 2


def test_skip_with_payments_keeps_paid_accounts_and_sweeps_the_rest(data, django_user_model):
    call_command('delete_test_users', prefix='e2e-', skip_with_payments=True)
    emails = set(django_user_model.objects.values_list('email', flat=True))
    assert 'e2e-paid@x.test' in emails and 'real@x.test' in emails
    assert 'e2e-plain@x.test' not in emails
    assert not Job.objects.exists()
    assert StudioBooking.all_objects.filter(customer=data['paid']).count() == 1


def test_dry_run_changes_nothing(data, django_user_model):
    call_command('delete_test_users', prefix='e2e-', dry_run=True)
    assert django_user_model.objects.filter(email__startswith='e2e-').count() == 2
