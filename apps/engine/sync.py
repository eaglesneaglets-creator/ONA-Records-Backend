"""
Mirror ONA's studio calendar into the booking engine ("mirror mode", step 2).

ONA stays in charge: every booking is still made, checked and guarded here.
The engine keeps an exact copy, so its availability can be compared with
ONA's in staging (step 3) before anything is switched over to it (step 4).

    ONA                                       Booking engine
    ───                                       ──────────────
    Room                                  ->  resource  room-<uuid>       kind "room"
    Engineer                              ->  resource  engineer-<uuid>   kind "engineer"
    opening hours (Business settings)     ->  every resource's weekly hours
    CalendarBlock                         ->  closure   block-<uuid>      kind "maintenance" / "holiday",
                                              on its room / engineer, or the whole studio
    StudioBooking, pending or confirmed   ->  booking   external_ref = the ONA reference,
                                              room + engineer, CONFIRMED in the engine

Pending ONA bookings are confirmed in the engine on purpose: ONA releases its
own unpaid bookings (apps/payments/checkout.py), and the release reaches the
engine as a cancel. A hold that expired in the engine first would make the
two calendars disagree.

Two ways changes get across:
  * at once: saving a room, engineer, block, booking or the business
    settings queues a sync after commit (signals.py);
  * every 30 minutes, reconcile() compares the two calendars and repairs
    anything a missed signal, a bulk update or an outage left behind, and
    reports what it had to fix, which is the drift to look at in staging.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.studio.models import SLOT_HOLDING_STATUSES, CalendarBlock, Engineer, Room, StudioBooking
from core.business import rules

from . import client
from .client import BookingEngineRefused

logger = logging.getLogger(__name__)

LIVE = ('held', 'confirmed')


# ── Refs ─────────────────────────────────────────────────────────────────

def room_ref(pk) -> str:
    return f'room-{pk}'


def engineer_ref(pk) -> str:
    return f'engineer-{pk}'


def block_ref(pk) -> str:
    return f'block-{pk}'


def _when(value: str) -> datetime:
    return parse_datetime(value)


# ── What ONA's rows look like in the engine ──────────────────────────────

def resource_payload(obj, kind: str) -> dict:
    return {'kind': kind, 'name': obj.name[:160], 'timezone': settings.TIME_ZONE,
            'is_active': bool(obj.is_active and obj.deleted_at is None)}


def schedule_payload() -> dict:
    r = rules()
    end = '24:00' if r.studio_close_hour >= 24 else f'{r.studio_close_hour:02d}:00'
    return {'weekly': [{'weekday': d, 'start': f'{r.studio_open_hour:02d}:00', 'end': end} for d in range(7)],
            'overrides': []}


def closure_payload(block: CalendarBlock) -> dict:
    resources = []
    if block.room_id:
        resources.append(room_ref(block.room_id))
    if block.engineer_id:
        resources.append(engineer_ref(block.engineer_id))
    return {'start': block.start_at.isoformat(), 'end': block.end_at.isoformat(), 'kind': block.kind,
            'reason': block.reason[:200], 'resources': resources}


def wanted(b: StudioBooking) -> bool:
    """Should the engine hold this booking's time?"""
    return b.deleted_at is None and b.status in SLOT_HOLDING_STATUSES


def booking_requirements(b: StudioBooking) -> list[dict]:
    return [{'resources': [room_ref(b.room_id)]}, {'resources': [engineer_ref(b.engineer_id)]}]


def booking_payload(b: StudioBooking) -> dict:
    return {'requirements': booking_requirements(b), 'start': b.start_at.isoformat(), 'end': b.end_at.isoformat(),
            'confirm': True, 'override_rules': True, 'external_ref': b.reference,
            'subject_ref': str(b.customer_id), 'metadata': {'ona_status': b.status}}


def same_time_and_place(engine: dict, b: StudioBooking) -> bool:
    return (_when(engine['start']) == b.start_at and _when(engine['end']) == b.end_at
            and sorted(r['ref'] for r in engine['resources']) == sorted([room_ref(b.room_id),
                                                                         engineer_ref(b.engineer_id)]))


def same_closure(engine: dict, block: CalendarBlock) -> bool:
    p = closure_payload(block)
    return (_when(engine['start']) == block.start_at and _when(engine['end']) == block.end_at
            and engine['kind'] == p['kind'] and engine['reason'] == p['reason']
            and sorted(engine['resources']) == sorted(p['resources']))


# ── One row at a time (from signals) ─────────────────────────────────────

def sync_resource(obj, kind: str) -> None:
    ref = room_ref(obj.pk) if kind == 'room' else engineer_ref(obj.pk)
    client.put_resource(ref, resource_payload(obj, kind))
    client.put_schedule(ref, schedule_payload())


def sync_schedules() -> int:
    """Opening hours changed: every room and engineer gets the new weekly hours."""
    n = 0
    for model, ref in ((Room, room_ref), (Engineer, engineer_ref)):
        for pk in model.all_objects.values_list('pk', flat=True):
            client.put_schedule(ref(pk), schedule_payload())
            n += 1
    return n


def sync_block(block: CalendarBlock) -> None:
    client.put_closure(block_ref(block.pk), closure_payload(block))


def remove_block(pk) -> None:
    client.delete_closure(block_ref(pk))


def sync_booking(b: StudioBooking) -> str:
    """Make the engine match this booking. Returns what was done; raises BookingEngineRefused on drift."""
    engine = client.find_booking(b.reference)
    live = engine is not None and engine['status'] in LIVE
    if wanted(b):
        if engine is None:
            client.create_booking(booking_payload(b), idempotency_key=f'ona-create-{b.reference}')
            return 'created'
        if not live:
            raise BookingEngineRefused(409, {'code': 'drift', 'detail': f'engine has it {engine["status"]}'})
        if not same_time_and_place(engine, b):
            client.reschedule_booking(engine['id'], {
                'start': b.start_at.isoformat(), 'end': b.end_at.isoformat(),
                'requirements': booking_requirements(b), 'override_rules': True})
            return 'moved'
        return 'unchanged'
    if live:
        client.cancel_booking(engine['id'], reason=f'ONA: {b.status}' if b.deleted_at is None else 'ONA: deleted')
        return 'cancelled'
    return 'unchanged'


# ── Everything at once ───────────────────────────────────────────────────

def reconcile(*, dry_run: bool = False) -> dict:
    """
    Compare ONA's calendar with the engine's and repair the engine.

    Covers every room and engineer, their hours, every block that has not
    ended, and every booking that has not ended. Safe to run any time and as
    often as wanted. With dry_run nothing is changed; the report says what
    would be. A row the engine refuses is reported and the run goes on; an
    unreachable engine stops the run (BookingEngineUnavailable).
    """
    now = timezone.now()
    report = {'resources': Counter(), 'closures': Counter(), 'bookings': Counter(), 'problems': []}

    def problem(what: str, exc: Exception) -> None:
        report['problems'].append(f'{what}: {exc}')

    # Resources and hours
    engine_resources = {r['ref']: r for r in client.list_resources()}
    for model, kind, ref in ((Room, 'room', room_ref), (Engineer, 'engineer', engineer_ref)):
        for obj in model.all_objects.all():
            payload, current = resource_payload(obj, kind), engine_resources.get(ref(obj.pk))
            if current is not None and all(current.get(k) == v for k, v in payload.items()):
                report['resources']['unchanged'] += 1
                continue
            report['resources']['created' if current is None else 'updated'] += 1
            if not dry_run:
                try:
                    sync_resource(obj, kind)
                except BookingEngineRefused as exc:
                    problem(f'{kind} {obj.name}', exc)
    if not dry_run:
        report['resources']['hours set'] = sync_schedules()

    # Blocks -> closures
    engine_closures = {c['external_ref']: c for c in client.list_closures() if c['external_ref'].startswith('block-')}
    blocks = list(CalendarBlock.objects.filter(end_at__gt=now))
    for block in blocks:
        current = engine_closures.get(block_ref(block.pk))
        if current is not None and same_closure(current, block):
            report['closures']['unchanged'] += 1
            continue
        report['closures']['created' if current is None else 'updated'] += 1
        if not dry_run:
            try:
                sync_block(block)
            except BookingEngineRefused as exc:
                problem(f'block {block}', exc)
    existing_blocks = {block_ref(pk) for pk in CalendarBlock.objects.values_list('pk', flat=True)}
    for ref in engine_closures.keys() - existing_blocks:
        report['closures']['removed'] += 1
        if not dry_run:
            remove_block(ref[len('block-'):])

    # Bookings
    ona = {b.reference: b for b in StudioBooking.objects.filter(status__in=SLOT_HOLDING_STATUSES, end_at__gt=now)}
    engine = {e['external_ref'] or e['id']: e for e in client.live_bookings(now.isoformat())}
    # Release what ONA does not hold FIRST: a stray in the engine could be
    # sitting exactly where a real ONA booking has to go.
    for ref, e in engine.items():
        if ref in ona:
            continue
        report['bookings']['cancelled'] += 1
        if not dry_run:
            try:
                client.cancel_booking(e['id'], reason='ONA does not hold this time')
            except BookingEngineRefused as exc:
                problem(f'engine booking {ref}', exc)
    for ref, b in ona.items():
        current = engine.get(ref)
        if current is not None and same_time_and_place(current, b):
            report['bookings']['unchanged'] += 1
            continue
        if dry_run:
            report['bookings']['missing' if current is None else 'moved'] += 1
            continue
        try:
            report['bookings'][sync_booking(b)] += 1
        except BookingEngineRefused as exc:
            problem(f'booking {ref}', exc)

    for p in report['problems']:
        logger.warning('Booking engine drift: %s', p)
    return report
