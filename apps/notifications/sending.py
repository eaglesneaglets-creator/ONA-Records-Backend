"""
Send one notification, exactly once, and record what happened.

Called from Celery tasks (apps/notifications/tasks.py), never from a request
directly — see dispatch.py for how a request hands work over.

Failure policy: a notification failing must never break the thing it is
about. Errors are caught, logged, and written to NotificationLog as FAILED.
A failed row with a dedupe_key can be claimed again later (the reminder
sweep does this naturally); a SENT one never is.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.utils import timezone

from .messages import Email
from .models import Channel, NotificationLog, Status

logger = logging.getLogger(__name__)

# A row left in SENDING this long belongs to a worker that died mid-send.
STALE_SENDING = timedelta(minutes=10)


def claim(kind: str, recipient: str, *, dedupe_key: str | None = None,
          booking=None) -> NotificationLog | None:
    """
    Reserve the right to send. Returns None if this notification has already
    gone (or is going) out — the caller must then do nothing.
    """
    defaults = dict(kind=kind, recipient=recipient, booking=booking, channel=Channel.EMAIL)
    if dedupe_key is None:
        return NotificationLog.objects.create(**defaults)
    try:
        with transaction.atomic():
            log, created = NotificationLog.objects.select_for_update().get_or_create(
                dedupe_key=dedupe_key, defaults=defaults)
            if created:
                return log
            if log.status == Status.SENT or log.status == Status.SKIPPED:
                return None
            if log.status == Status.SENDING and timezone.now() - log.updated_at < STALE_SENDING:
                return None
            log.status = Status.SENDING
            log.attempts += 1
            log.save(update_fields=['status', 'attempts', 'updated_at'])
            return log
    except IntegrityError:
        return None   # another worker created it between our read and write


def deliver(log: NotificationLog, email: Email) -> bool:
    msg = EmailMultiAlternatives(
        subject=email.subject, body=email.text,
        from_email=settings.DEFAULT_FROM_EMAIL, to=[log.recipient],
    )
    msg.attach_alternative(email.html, 'text/html')
    for filename, content, mimetype in email.attachments:
        msg.attach(filename, content, mimetype)
    try:
        msg.send(fail_silently=False)
    except Exception as exc:  # SMTP down, bad address, redirect not configured…
        logger.exception('Notification %s to %s failed', log.kind, log.recipient)
        log.status = Status.FAILED
        log.error = f'{type(exc).__name__}: {exc}'[:500]
        log.save(update_fields=['status', 'error', 'updated_at'])
        return False
    log.status = Status.SENT
    log.sent_at = timezone.now()
    log.error = ''
    log.save(update_fields=['status', 'sent_at', 'error', 'updated_at'])
    return True


def skip(log: NotificationLog, reason: str) -> None:
    log.status = Status.SKIPPED
    log.error = reason[:500]
    log.save(update_fields=['status', 'error', 'updated_at'])
