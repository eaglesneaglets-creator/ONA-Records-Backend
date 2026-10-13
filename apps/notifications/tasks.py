"""
Celery tasks for notifications.

Tasks take ids, not objects or links: arguments sit in the broker, and a
password-reset link in Redis is a credential at rest. The worker builds the
link itself from the user id.
"""

from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from . import messages
from .models import Kind
from .sending import claim, deliver, skip

# How long before a session the reminder goes out.
REMINDER_HOURS = 24
# A booking made this recently has a fresh confirmation in the inbox; a
# reminder on top of it is noise.
REMINDER_MIN_AGE = timedelta(hours=6)


def _booking(booking_id):
    from apps.studio.models import StudioBooking
    return (StudioBooking.all_objects.select_related('customer', 'room', 'engineer', 'service')
            .filter(pk=booking_id).first())


@shared_task(ignore_result=True)
def send_booking_notification(booking_id: str, kind: str) -> str:
    booking = _booking(booking_id)
    if booking is None:
        return 'missing'
    to = booking.customer.email

    if kind == Kind.BOOKING_RECEIVED:
        key, build = f'{kind}:{booking.pk}', lambda: messages.booking_received(booking)
    elif kind == Kind.BOOKING_RESCHEDULED:
        key = f'{kind}:{booking.pk}:{booking.start_at.isoformat()}'
        build = lambda: messages.booking_rescheduled(booking)  # noqa: E731
    elif kind == Kind.BOOKING_CANCELLED:
        key, build = f'{kind}:{booking.pk}', lambda: messages.booking_cancelled(booking)
    elif kind == Kind.PAYMENT_CONFIRMED:
        from apps.payments.models import Receipt
        receipt = (Receipt.objects.filter(transaction__booking=booking)
                   .order_by('-issued_at').first())
        if receipt is None:
            return 'no-receipt'
        key = f'{kind}:{receipt.number}'
        build = lambda: messages.payment_confirmed(booking, receipt)  # noqa: E731
    else:
        raise ValueError(f'Unknown booking notification kind: {kind!r}')

    log = claim(kind, to, dedupe_key=key, booking=booking)
    if log is None:
        return 'already-sent'
    return 'sent' if deliver(log, build()) else 'failed'


@shared_task(ignore_result=True)
def send_reminder(booking_id: str) -> str:
    booking = _booking(booking_id)
    if booking is None:
        return 'missing'
    # Keyed on the start time: a rescheduled session gets its own reminder,
    # and the old time's reminder can never go out for the new one.
    log = claim(Kind.REMINDER, booking.customer.email,
                dedupe_key=f'reminder:{booking.pk}:{booking.start_at.isoformat()}',
                booking=booking)
    if log is None:
        return 'already-sent'
    # Re-checked at send time: the booking may have changed since the sweep.
    if not booking.holds_slot or booking.is_deleted or booking.start_at <= timezone.now():
        skip(log, 'Booking no longer upcoming when the reminder ran.')
        return 'skipped'
    return 'sent' if deliver(log, messages.reminder(booking)) else 'failed'


@shared_task(ignore_result=True)
def send_due_reminders() -> int:
    """
    The sweep, run every 15 minutes by Celery beat (or `manage.py
    send_reminders` from a cron). Finds sessions starting within
    REMINDER_HOURS that have not been reminded at their current start time.
    """
    from apps.notifications.models import NotificationLog, Status
    from apps.studio.models import SLOT_HOLDING_STATUSES, StudioBooking

    now = timezone.now()
    due = StudioBooking.objects.filter(
        status__in=SLOT_HOLDING_STATUSES,
        start_at__gt=now,
        start_at__lte=now + timedelta(hours=REMINDER_HOURS),
        created_at__lte=now - REMINDER_MIN_AGE,
    ).values_list('pk', 'start_at')
    sent_keys = set(NotificationLog.objects.filter(
        kind=Kind.REMINDER, status__in=[Status.SENT, Status.SKIPPED],
        booking_id__in=[pk for pk, _ in due],
    ).values_list('dedupe_key', flat=True))
    queued = 0
    for pk, start in due:
        if f'reminder:{pk}:{start.isoformat()}' in sent_keys:
            continue
        send_reminder.delay(str(pk))
        queued += 1
    return queued


@shared_task(ignore_result=True)
def send_account_email(user_id: str, kind: str) -> str:
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    from apps.accounts.tokens import email_verification_token

    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        return 'missing'
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    if kind == Kind.VERIFY_EMAIL:
        token = email_verification_token.make_token(user)
        email = messages.verify_email(user, f'{settings.FRONTEND_URL}/verify-email?uid={uid}&token={token}')
    elif kind == Kind.PASSWORD_RESET:
        token = default_token_generator.make_token(user)
        email = messages.password_reset(user, f'{settings.FRONTEND_URL}/reset-password?uid={uid}&token={token}')
    else:
        raise ValueError(f'Unknown account email kind: {kind!r}')
    # No dedupe key: asking twice for a reset link is two legitimate emails.
    log = claim(kind, user.email)
    return 'sent' if deliver(log, email) else 'failed'
