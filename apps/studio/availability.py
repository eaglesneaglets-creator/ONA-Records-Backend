"""
Availability — "what is free for this room / engineer between these dates?"

Answers in hour slots inside studio opening hours, each labelled with one
of the four SRS states: available / booked / maintenance / holiday. The
booking form's Date & Time step consumes this directly; it never has to
reason about overlap itself.

PRECEDENCE when a slot is covered by more than one thing: holiday beats
maintenance beats booked. A holiday is the studio being shut — a booking
that somehow exists inside one is a data problem for staff, not something
to show a customer as bookable or merely "booked".

No customer identity leaves here. "Booked" is all a visitor learns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import BlockKind, CalendarBlock, SLOT_HOLDING_STATUSES, StudioBooking

STUDIO_TZ = ZoneInfo(settings.TIME_ZONE)

# Opening hours. Configurable by ONA later (Phase 3 content management);
# a constant is correct until someone needs to change it.
OPEN_HOUR = 9
CLOSE_HOUR = 21
SLOT_MINUTES = 60
MAX_RANGE_DAYS = 31

AVAILABLE, BOOKED, MAINTENANCE, HOLIDAY = 'available', 'booked', 'maintenance', 'holiday'
# Not one of the four SRS states: a slot that has already started. Shown as
# unavailable rather than 'available' — the booking endpoint would refuse it.
PAST = 'past'


@dataclass(frozen=True)
class Slot:
    start_at: datetime
    end_at: datetime
    state: str

    def as_dict(self) -> dict:
        return {'start_at': self.start_at, 'end_at': self.end_at, 'state': self.state}


def day_slots(day: date) -> list[tuple[datetime, datetime]]:
    """Opening-hours slots for one calendar day, in the studio timezone."""
    start = datetime.combine(day, time(OPEN_HOUR), tzinfo=STUDIO_TZ)
    end = datetime.combine(day, time(CLOSE_HOUR), tzinfo=STUDIO_TZ)
    out = []
    cursor = start
    step = timedelta(minutes=SLOT_MINUTES)
    while cursor + step <= end:
        out.append((cursor, cursor + step))
        cursor += step
    return out


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    # Half-open intervals: touching ends do not overlap.
    return a_start < b_end and b_start < a_end


def availability(*, start: date, end: date, room=None, engineer=None) -> list[Slot]:
    """
    Slots from `start` to `end` inclusive, for a room, an engineer, or both.

    With both given, a slot is free only if both are free — that is what a
    booking needs. With neither, only studio-wide holidays apply, which is
    enough for a first "pick a date" view.
    """
    if end < start:
        raise ValueError('end before start')
    if (end - start).days >= MAX_RANGE_DAYS:
        raise ValueError(f'range longer than {MAX_RANGE_DAYS} days')

    window_start = datetime.combine(start, time.min, tzinfo=STUDIO_TZ)
    window_end = datetime.combine(end + timedelta(days=1), time.min, tzinfo=STUDIO_TZ)

    # One query each, then overlap in Python — the window is at most a
    # month of hour slots, which is small, and keeps the rule readable.
    scope = Q(room__isnull=True, engineer__isnull=True)       # studio-wide
    if room is not None:
        scope |= Q(room=room)
    if engineer is not None:
        scope |= Q(engineer=engineer)
    blocks = list(
        CalendarBlock.objects.filter(scope, start_at__lt=window_end, end_at__gt=window_start)
        .values_list('kind', 'start_at', 'end_at')
    )

    bookings: list[tuple] = []
    if room is not None or engineer is not None:
        who = Q()
        if room is not None:
            who |= Q(room=room)
        if engineer is not None:
            who |= Q(engineer=engineer)
        bookings = list(
            StudioBooking.objects.filter(
                who, status__in=SLOT_HOLDING_STATUSES,
                start_at__lt=window_end, end_at__gt=window_start,
            ).values_list('start_at', 'end_at')
        )

    now = timezone.now()
    slots: list[Slot] = []
    day = start
    while day <= end:
        for s, e in day_slots(day):
            state = AVAILABLE if s > now else PAST
            for kind, bs, be in blocks:
                if _overlaps(s, e, bs, be):
                    if kind == BlockKind.HOLIDAY:
                        state = HOLIDAY
                        break
                    state = MAINTENANCE
            if state in (AVAILABLE, PAST):
                for bs, be in bookings:
                    if _overlaps(s, e, bs, be):
                        state = BOOKED
                        break
            slots.append(Slot(s, e, state))
        day += timedelta(days=1)
    return slots


def is_free(*, room, engineer, start_at: datetime, end_at: datetime,
            exclude_booking=None) -> tuple[bool, str]:
    """
    Pre-insert check for the booking endpoint: (ok, reason).

    This exists so the customer gets a sentence instead of a 500. It is NOT
    the guarantee — the exclusion constraint is. Two requests can both pass
    here at the same instant; only one will commit.
    """
    if end_at <= start_at:
        return False, 'The session must end after it starts.'

    block = CalendarBlock.objects.filter(
        Q(room__isnull=True, engineer__isnull=True) | Q(room=room) | Q(engineer=engineer),
        start_at__lt=end_at, end_at__gt=start_at,
    ).first()
    if block is not None:
        return False, f'The studio is closed for {block.get_kind_display().lower()} during that time.'

    clash = StudioBooking.objects.filter(
        Q(room=room) | Q(engineer=engineer),
        status__in=SLOT_HOLDING_STATUSES,
        start_at__lt=end_at, end_at__gt=start_at,
    )
    if exclude_booking is not None:
        # Rescheduling: a booking does not clash with its own old slot.
        clash = clash.exclude(pk=exclude_booking.pk)
    clash = clash.first()
    if clash is not None:
        which = 'room' if clash.room_id == room.id else 'engineer'
        return False, f'That {which} is already booked for part of that time.'

    return True, ''
