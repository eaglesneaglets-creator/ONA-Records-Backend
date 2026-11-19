"""
Queue a mirror sync whenever a studio row changes (see sync.py).

After commit only, so a change that rolls back is never sent. Never breaks
the save that triggered it: handing work to Celery is wrapped like
notifications' dispatch, and nothing happens until the engine is configured.
"""

import logging

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.notifications.dispatch import _broker_available
from apps.studio.models import CalendarBlock, Engineer, Room, StudioBooking
from core.models import BusinessSettings

from . import client, tasks

logger = logging.getLogger(__name__)


def _enqueue(task, *args) -> None:
    if not client.is_configured():
        return

    def run():
        try:
            if _broker_available():
                task.delay(*args)
            else:
                task.apply(args=args)
        except Exception:
            logger.exception('Could not queue %s%r for the booking engine', task.name, args)
    transaction.on_commit(run)


@receiver(post_save, sender=Room)
def room_saved(sender, instance, raw=False, **kwargs):
    if not raw:
        _enqueue(tasks.sync_resource, 'room', str(instance.pk))


@receiver(post_save, sender=Engineer)
def engineer_saved(sender, instance, raw=False, **kwargs):
    if not raw:
        _enqueue(tasks.sync_resource, 'engineer', str(instance.pk))


@receiver(post_save, sender=CalendarBlock)
@receiver(post_delete, sender=CalendarBlock)
def block_changed(sender, instance, raw=False, **kwargs):
    if not raw:
        _enqueue(tasks.sync_block, str(instance.pk))


@receiver(post_save, sender=StudioBooking)
@receiver(post_delete, sender=StudioBooking)
def booking_changed(sender, instance, raw=False, **kwargs):
    if not raw:
        _enqueue(tasks.sync_booking, str(instance.pk))


@receiver(post_save, sender=BusinessSettings)
def hours_maybe_changed(sender, instance, raw=False, **kwargs):
    if not raw:
        _enqueue(tasks.sync_schedules)
