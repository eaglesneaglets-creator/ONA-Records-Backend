"""
How the rest of the code asks for a notification.

    notify_booking(booking, Kind.BOOKING_RECEIVED)
    notify_account(user, Kind.PASSWORD_RESET)

Three guarantees:

  1. **After commit.** Queued with transaction.on_commit, so an email is
     never sent about a booking that then rolled back.
  2. **Never breaks the caller.** Any error handing work to Celery is logged
     and swallowed: a broker outage must not fail a booking or a login.
  3. **Works without a broker.** If no broker is configured (staging before
     Redis is added) the task runs inline after the response's commit,
     instead of hanging the request on a connection that will never open.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)


def _broker_available() -> bool:
    return bool(getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False)
                or getattr(settings, 'CELERY_BROKER_URL', ''))


def _enqueue(task, *args) -> None:
    def run():
        try:
            if _broker_available():
                task.delay(*args)
            else:
                logger.warning('No Celery broker configured; sending %s inline.', task.name)
                task.apply(args=args)
        except Exception:
            logger.exception('Could not hand %s%r to the worker', task.name, args)
    transaction.on_commit(run)


def notify_booking(booking, kind: str) -> None:
    from .tasks import send_booking_notification
    _enqueue(send_booking_notification, str(booking.pk), kind)


def notify_account(user, kind: str) -> None:
    from .tasks import send_account_email
    _enqueue(send_account_email, str(user.pk), kind)
