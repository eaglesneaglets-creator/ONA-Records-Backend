"""
Background mirroring to the booking engine. Each task retries with backoff
while the engine is unreachable; the 30-minute reconcile repairs anything
that still slipped through.
"""

import logging

from celery import shared_task

from . import client, sync
from .client import BookingEngineRefused, BookingEngineUnavailable

logger = logging.getLogger(__name__)
RETRY = {'autoretry_for': (BookingEngineUnavailable,), 'retry_backoff': 30, 'retry_backoff_max': 600,
         'max_retries': 6}


def _refused(what: str, exc: BookingEngineRefused) -> None:
    # Not retried: the engine will say the same again. reconcile() reports it too.
    logger.warning('Booking engine refused %s: %s', what, exc)


@shared_task(**RETRY)
def sync_resource(kind: str, pk: str) -> None:
    from apps.studio.models import Engineer, Room
    obj = (Room if kind == 'room' else Engineer).all_objects.filter(pk=pk).first()
    if obj is not None:
        try:
            sync.sync_resource(obj, kind)
        except BookingEngineRefused as exc:
            _refused(f'{kind} {pk}', exc)


@shared_task(**RETRY)
def sync_schedules() -> int:
    return sync.sync_schedules()


@shared_task(**RETRY)
def sync_block(pk: str) -> None:
    from apps.studio.models import CalendarBlock
    block = CalendarBlock.objects.filter(pk=pk).first()
    try:
        if block is None:
            sync.remove_block(pk)
        else:
            sync.sync_block(block)
    except BookingEngineRefused as exc:
        _refused(f'block {pk}', exc)


@shared_task(**RETRY)
def sync_booking(pk: str) -> str:
    from apps.studio.models import StudioBooking
    b = StudioBooking.all_objects.select_related('room', 'engineer').filter(pk=pk).first()
    if b is None:
        return 'gone'
    try:
        return sync.sync_booking(b)
    except BookingEngineRefused as exc:
        _refused(f'booking {b.reference}', exc)
        return 'refused'


@shared_task
def reconcile() -> dict | None:
    if not client.is_configured():
        return None
    report = sync.reconcile()
    return {k: dict(v) if hasattr(v, 'items') else v for k, v in report.items()}
