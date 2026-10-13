"""
Task 11 — confirmation emails and reminders.

What must hold:
  * each booking event emails the customer once, after the database commits;
  * a confirmation carries the invoice, a payment email carries the receipt;
  * reminders go out once per session time — not twice, not for cancelled
    or moved-away sessions, not for a booking made an hour ago;
  * a mail failure never breaks the booking, and is recorded;
  * account emails leave the request thread and never put a token in the broker.
"""

from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.notifications.models import Kind, NotificationLog, Status
from apps.notifications.tasks import (
    REMINDER_MIN_AGE, send_booking_notification, send_due_reminders, send_reminder,
)
from apps.payments.ledger import mark_succeeded, open_transaction
from apps.studio.models import BookingStatus, Engineer, Room, Service, StudioBooking

pytestmark = pytest.mark.django_db

TZ = ZoneInfo('Africa/Accra')


@pytest.fixture
def world(django_user_model):
    return {
        'me': django_user_model.objects.create_user(
            email='kofi@x.test', password='p-Ass-1', role='customer', first_name="Kofi O'Neil"),
        'room': Room.objects.create(name='Studio A — Live Room', hourly_rate_pesewas=20000),
        'eng': Engineer.objects.create(name='Engineer One'),
        'svc': Service.objects.create(name='Recording session', base_price_pesewas=1,
                                      duration_minutes=60),
    }


def at(days, hour):
    d = date.today() + timedelta(days=days)
    return datetime.combine(d, datetime.min.time(), tzinfo=TZ) + timedelta(hours=hour)


def booking(w, start, hours=2, **kw):
    return StudioBooking.objects.create(
        customer=w['me'], room=w['room'], engineer=w['eng'], service=w['svc'],
        start_at=start, end_at=start + timedelta(hours=hours), total_pesewas=20000 * hours, **kw)


def api(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def age(b, hours):
    StudioBooking.objects.filter(pk=b.pk).update(created_at=timezone.now() - timedelta(hours=hours))
    b.refresh_from_db()


class TestBookingEmails:
    def test_booking_sends_one_confirmation_with_the_invoice(self, world,
                                                             django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            res = api(world['me']).post(reverse('studio:booking-list'), {
                'room': world['room'].id, 'engineer': world['eng'].id,
                'service': world['svc'].id, 'start_at': at(5, 10).isoformat(), 'hours': 2},
                format='json')
        assert res.status_code == 201
        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        assert msg.to == ['kofi@x.test']
        assert res.data['reference'] in msg.subject
        assert 'GHS 400.00' in msg.body and 'No payment has been taken' in msg.body
        assert "Kofi O'Neil" in msg.body                      # not HTML-escaped in plain text
        (name, content, mimetype), = msg.attachments
        assert name.startswith('ONA-invoice-INV-') and mimetype == 'application/pdf'
        assert content.startswith(b'%PDF')
        html, html_type = msg.alternatives[0]
        assert html_type == 'text/html' and 'Kofi O&#x27;Neil' in html   # escaped once, in HTML

    def test_nothing_is_sent_if_the_booking_rolls_back(self, world,
                                                       django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            api(world['me']).post(reverse('studio:booking-list'), {
                'room': world['room'].id, 'engineer': world['eng'].id,
                'service': world['svc'].id, 'start_at': at(5, 10).isoformat(), 'hours': 2},
                format='json')
        assert len(callbacks) == 1 and mail.outbox == []     # queued for commit, not sent

    def test_cancel_and_reschedule_each_email_once(self, world,
                                                   django_capture_on_commit_callbacks):
        b = booking(world, at(5, 10))
        with django_capture_on_commit_callbacks(execute=True):
            api(world['me']).post(reverse('studio:booking-reschedule', args=[b.id]),
                                  {'start_at': at(5, 14).isoformat()}, format='json')
            api(world['me']).post(reverse('studio:booking-cancel', args=[b.id]))
        subjects = [m.subject for m in mail.outbox]
        assert subjects == [f'Your session has moved — {b.reference}',
                            f'Booking cancelled — {b.reference}']
        assert '14:00' in mail.outbox[0].body

    def test_a_repeated_event_does_not_email_twice(self, world):
        b = booking(world, at(5, 10))
        assert send_booking_notification(str(b.pk), Kind.BOOKING_RECEIVED) == 'sent'
        assert send_booking_notification(str(b.pk), Kind.BOOKING_RECEIVED) == 'already-sent'
        assert len(mail.outbox) == 1

    def test_payment_email_carries_the_receipt_once(self, world,
                                                    django_capture_on_commit_callbacks):
        b = booking(world, at(5, 10))
        txn = open_transaction(b)
        with django_capture_on_commit_callbacks(execute=True):
            mark_succeeded(txn, provider_ref='HUB-1')
            mark_succeeded(txn, provider_ref='HUB-1')          # replayed callback
        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        assert 'GHS 400.00' in msg.body and 'holds the money' in msg.body
        assert msg.attachments[0][0].startswith('ONA-receipt-RCT-')

    def test_a_mail_failure_never_breaks_the_booking(self, world,
                                                     django_capture_on_commit_callbacks):
        with mock.patch('django.core.mail.EmailMultiAlternatives.send',
                        side_effect=ConnectionError('SMTP down')):
            with django_capture_on_commit_callbacks(execute=True):
                res = api(world['me']).post(reverse('studio:booking-list'), {
                    'room': world['room'].id, 'engineer': world['eng'].id,
                    'service': world['svc'].id, 'start_at': at(5, 10).isoformat(),
                    'hours': 2}, format='json')
        assert res.status_code == 201
        log = NotificationLog.objects.get(kind=Kind.BOOKING_RECEIVED)
        assert log.status == Status.FAILED and 'SMTP down' in log.error

    def test_without_a_broker_it_sends_inline_instead_of_hanging(self, world, settings,
                                                                 django_capture_on_commit_callbacks):
        settings.CELERY_TASK_ALWAYS_EAGER = False
        settings.CELERY_BROKER_URL = ''
        b = booking(world, at(5, 10))
        with mock.patch('apps.notifications.tasks.send_booking_notification.delay') as delay:
            with django_capture_on_commit_callbacks(execute=True):
                from apps.notifications.dispatch import notify_booking
                notify_booking(b, Kind.BOOKING_RECEIVED)
        delay.assert_not_called()
        assert len(mail.outbox) == 1


class TestReminders:
    def test_sends_once_for_a_session_inside_24h(self, world):
        b = booking(world, timezone.now().replace(minute=0, second=0, microsecond=0)
                    + timedelta(hours=20))
        age(b, 48)
        assert send_due_reminders() == 1
        assert send_due_reminders() == 0                       # next sweep: already sent
        assert len(mail.outbox) == 1
        assert b.reference in mail.outbox[0].subject

    def test_ignores_sessions_further_out_and_fresh_bookings(self, world):
        far = booking(world, at(5, 10))
        age(far, 48)
        soon_new = booking(world, timezone.now().replace(minute=0, second=0, microsecond=0)
                           + timedelta(hours=10))
        age(soon_new, (REMINDER_MIN_AGE - timedelta(hours=1)).total_seconds() / 3600)
        assert send_due_reminders() == 0

    def test_cancelled_sessions_are_not_reminded(self, world):
        b = booking(world, timezone.now() + timedelta(hours=10), status=BookingStatus.CANCELLED)
        age(b, 48)
        assert send_due_reminders() == 0

    def test_rescheduled_session_gets_a_new_reminder(self, world):
        start = timezone.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=20)
        b = booking(world, start)
        age(b, 48)
        send_due_reminders()
        StudioBooking.objects.filter(pk=b.pk).update(start_at=start + timedelta(hours=2),
                                                     end_at=start + timedelta(hours=4))
        assert send_due_reminders() == 1
        assert len(mail.outbox) == 2

    def test_a_reminder_rechecks_the_booking_when_it_runs(self, world):
        b = booking(world, timezone.now() + timedelta(hours=10))
        StudioBooking.objects.filter(pk=b.pk).update(status=BookingStatus.CANCELLED)
        assert send_reminder(str(b.pk)) == 'skipped'
        assert mail.outbox == []
        assert NotificationLog.objects.get(kind=Kind.REMINDER).status == Status.SKIPPED

    def test_management_command(self, world):
        from django.core.management import call_command
        b = booking(world, timezone.now().replace(minute=0, second=0, microsecond=0)
                    + timedelta(hours=20))
        age(b, 48)
        call_command('send_reminders')
        assert len(mail.outbox) == 1


class TestAccountEmails:
    def test_password_reset_email_comes_from_the_worker(self, world,
                                                        django_capture_on_commit_callbacks):
        with mock.patch('apps.notifications.tasks.send_account_email.delay') as delay:
            with django_capture_on_commit_callbacks(execute=True):
                res = APIClient().post(reverse('accounts:password-reset'),
                                       {'email': 'kofi@x.test'}, format='json')
        assert res.status_code == 200
        # Only the user id goes to the broker — never the reset link.
        delay.assert_called_once_with(str(world['me'].pk), Kind.PASSWORD_RESET)

    def test_password_reset_email_content(self, world, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            APIClient().post(reverse('accounts:password-reset'), {'email': 'kofi@x.test'},
                             format='json')
        assert len(mail.outbox) == 1
        assert '/reset-password?uid=' in mail.outbox[0].body

    def test_unknown_address_sends_nothing_and_answers_the_same(self, world,
                                                                django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            res = APIClient().post(reverse('accounts:password-reset'),
                                   {'email': 'nobody@x.test'}, format='json')
        assert res.status_code == 200 and mail.outbox == []

    def test_registration_sends_the_verification_link(self, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            res = APIClient().post(reverse('accounts:register'), {
                'email': 'new@x.test', 'password': 'Studio-Session-2026!',
                'password_confirm': 'Studio-Session-2026!', 'first_name': 'Ama',
                'last_name': 'B', 'role': 'customer'}, format='json')
        assert res.status_code == 201
        assert len(mail.outbox) == 1 and '/verify-email?uid=' in mail.outbox[0].body
