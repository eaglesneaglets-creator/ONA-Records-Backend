"""
Studio catalogue — rooms, engineers and services.

Everything here is admin-managed data, never hardcoded: the SRS is explicit
that ONA configures its own studio. The real catalogue is still to come from
ONA (see .paul/STATE.md); until then `seed_studio` loads placeholders.

MONEY

Every amount is an integer number of pesewas, named with a `_pesewas` suffix
so the unit is visible at every call site. See apps/payments/money.py for
why: this ledger decides what people are paid, and float arithmetic drifts.

ACTIVE vs DELETED

Two different questions, two different mechanisms:

  is_active=False   ONA has taken the room out of service for now. It stays
                    visible in admin, keeps its history, and can be switched
                    back on. Hidden from customers.
  soft_delete()     The row is gone from everywhere except audit surfaces.

Public endpoints use `.objects.active()`, which answers both at once.

Bookings and the calendar live in a later migration (Task 4/5); keeping the
catalogue on its own first lets the seeder and admin land without them.
"""

import secrets

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeOperators
from django.contrib.postgres.fields.ranges import DateTimeRangeField  # noqa: F401 (TsTzRange below)
from django.core.validators import MinValueValidator
from django.db import IntegrityError, models, transaction
from django.db.models import Func


class TsTzRange(Func):
    """tstzrange(start, end) — half-open [start, end), so a booking ending at
    14:00 and one starting at 14:00 do not overlap."""
    function = 'tstzrange'
    output_field = DateTimeRangeField()


from apps.payments.money import format_ghs  # noqa: E402
from core.mixins.soft_delete import AllObjectsManager, SoftDeleteManager, SoftDeleteMixin
from core.mixins.timestamp import TimestampMixin, UUIDPrimaryKeyMixin


class CatalogueQuerySet(models.QuerySet):
    def active(self):
        """What a customer may see and book. Soft-deleted rows are already
        excluded by the manager; this adds the is_active switch."""
        return self.filter(is_active=True)


class CatalogueItem(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """
    Shared shape of every catalogue row.

    `objects` is declared first on purpose — see SoftDeleteMixin. Re-declared
    here (rather than inherited) so it carries CatalogueQuerySet.
    """

    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(
        default=True, db_index=True,
        help_text='Off = hidden from customers but kept, with history, in admin.',
    )
    sort_order = models.PositiveSmallIntegerField(
        default=100,
        help_text='Lower first. Ties fall back to name.',
    )

    objects = SoftDeleteManager.from_queryset(CatalogueQuerySet)()
    all_objects = AllObjectsManager()

    class Meta:
        abstract = True
        ordering = ('sort_order', 'name')
        constraints = [
            # Unique among live rows. A plain unique=True clashed with soft
            # delete: admin's form check cannot see deleted rows, so reusing a
            # deleted room's name passed validation and then failed the insert.
            models.UniqueConstraint(
                fields=['name'], condition=models.Q(deleted_at__isnull=True),
                name='%(app_label)s_%(class)s_name_live_unique',
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Room(CatalogueItem):
    hourly_rate_pesewas = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text='Integer pesewas. GHS 200.00 = 20000.',
    )
    capacity = models.PositiveSmallIntegerField(
        default=1,
        help_text='How many people the room comfortably holds.',
    )

    @property
    def hourly_rate_display(self) -> str:
        return format_ghs(self.hourly_rate_pesewas)


class Engineer(CatalogueItem):
    """
    A studio engineer ONA assigns to sessions.

    Not a User and not a marketplace Professional: engineers are ONA staff
    who appear on the booking form, not accounts that log in. If an engineer
    ever needs a login that is a separate link added then, not a rename now.
    """

    bio = models.TextField(blank=True)
    specialties = models.JSONField(
        default=list, blank=True,
        help_text='List of strings, e.g. ["mixing", "vocal production"].',
    )


class Service(CatalogueItem):
    base_price_pesewas = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text='Integer pesewas for one unit of `duration_minutes`.',
    )
    duration_minutes = models.PositiveIntegerField(
        validators=[MinValueValidator(15)],
        help_text='Default session length. Bookings may extend it in whole units.',
    )

    @property
    def base_price_display(self) -> str:
        return format_ghs(self.base_price_pesewas)


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

class BlockKind(models.TextChoices):
    MAINTENANCE = 'maintenance', 'Maintenance'
    HOLIDAY = 'holiday', 'Holiday'


class CalendarBlock(UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Time ONA has taken off the calendar for a reason that is not a booking.

    Scope is by what is null: a block with a room applies to that room, with
    an engineer to that engineer, with neither to the whole studio (a public
    holiday). Both set means "this engineer in this room", which is rarely
    what staff mean — admin validation steers them to one or the other.

    Blocks do not use the exclusion constraint: two maintenance windows may
    overlap harmlessly. They simply remove time from availability.
    """

    kind = models.CharField(max_length=16, choices=BlockKind.choices)
    room = models.ForeignKey(Room, null=True, blank=True, on_delete=models.CASCADE,
                             related_name='blocks')
    engineer = models.ForeignKey(Engineer, null=True, blank=True, on_delete=models.CASCADE,
                                 related_name='blocks')
    start_at = models.DateTimeField(db_index=True)
    end_at = models.DateTimeField()
    reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ('start_at',)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_at__gt=models.F('start_at')),
                name='studio_block_ends_after_start',
            ),
        ]

    def __str__(self) -> str:
        scope = self.room or self.engineer or 'studio'
        return f'{self.get_kind_display()} — {scope} {self.start_at:%Y-%m-%d %H:%M}'


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------

class BookingStatus(models.TextChoices):
    PENDING = 'pending', 'Pending payment'
    CONFIRMED = 'confirmed', 'Confirmed'
    CANCELLED = 'cancelled', 'Cancelled'
    COMPLETED = 'completed', 'Completed'


# Statuses that hold a slot. PENDING is included deliberately: a customer
# who has started paying must not lose the slot to someone who clicks
# faster, and two people must never pay for the same hour. Phase 2 expires
# stale pending bookings; until then admin cancels them.
SLOT_HOLDING_STATUSES = (BookingStatus.PENDING, BookingStatus.CONFIRMED)


def constraint_name(exc: Exception) -> str:
    """The Postgres constraint an IntegrityError came from, or ''."""
    diag = getattr(getattr(exc, '__cause__', None), 'diag', None)
    return getattr(diag, 'constraint_name', None) or ''


def generate_reference() -> str:
    """ONA-7F3A2C — short, unambiguous, not sequential (see UUIDPrimaryKeyMixin
    for why volume must not leak). Collision is handled at save time."""
    return 'ONA-' + secrets.token_hex(3).upper()


class StudioBooking(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """
    A customer's hold on a room and an engineer for a span of time.

    THE DOUBLE-BOOKING RULE LIVES IN THE DATABASE

    Two exclusion constraints below make it impossible — not merely
    unlikely — for two slot-holding bookings to overlap on the same room, or
    on the same engineer. Application code validates first so the customer
    gets a readable message, but the constraint is what guarantees it under
    concurrency: two requests that both pass validation at the same instant
    cannot both commit. Postgres `EXCLUDE USING gist` with `btree_gist`; see
    docs/learning/postgres-exclusion-constraints.md.

    Money: `total_pesewas` is computed at creation from the room rate and
    duration, and frozen. A later price change must not re-price a booking
    someone already agreed to.
    """

    customer = models.ForeignKey('accounts.User', on_delete=models.PROTECT,
                                 related_name='studio_bookings')
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name='bookings')
    engineer = models.ForeignKey(Engineer, on_delete=models.PROTECT, related_name='bookings')
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name='bookings')

    start_at = models.DateTimeField(db_index=True)
    end_at = models.DateTimeField()
    status = models.CharField(max_length=12, choices=BookingStatus.choices,
                              default=BookingStatus.PENDING, db_index=True)
    reference = models.CharField(max_length=12, unique=True, default=generate_reference,
                                 editable=False)
    total_pesewas = models.PositiveIntegerField(help_text='Frozen at creation. Integer pesewas.')
    notes = models.TextField(blank=True, help_text='From the customer, for the engineer.')

    class Meta:
        ordering = ('-start_at',)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_at__gt=models.F('start_at')),
                name='studio_booking_ends_after_start',
            ),
            ExclusionConstraint(
                name='studio_booking_no_room_overlap',
                expressions=[
                    ('room', RangeOperators.EQUAL),
                    (TsTzRange('start_at', 'end_at'), RangeOperators.OVERLAPS),
                ],
                condition=models.Q(status__in=SLOT_HOLDING_STATUSES, deleted_at__isnull=True),
                violation_error_message='That room is already booked for part of this time.',
            ),
            ExclusionConstraint(
                name='studio_booking_no_engineer_overlap',
                expressions=[
                    ('engineer', RangeOperators.EQUAL),
                    (TsTzRange('start_at', 'end_at'), RangeOperators.OVERLAPS),
                ],
                condition=models.Q(status__in=SLOT_HOLDING_STATUSES, deleted_at__isnull=True),
                violation_error_message='That engineer is already booked for part of this time.',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.reference} · {self.room} · {self.start_at:%Y-%m-%d %H:%M}'

    @property
    def holds_slot(self) -> bool:
        return self.status in SLOT_HOLDING_STATUSES

    @property
    def can_change(self) -> bool:
        """
        May the customer still cancel or move this booking?

        Any time before the session starts, while it holds a slot. This is the
        v0.1 rule while no money moves; ONA sets the real cancellation window
        (and what is refunded) with payments in Phase 2.
        """
        from django.utils import timezone
        return self.holds_slot and self.start_at > timezone.now() and not self.is_deleted

    @property
    def duration_minutes(self) -> int:
        return int((self.end_at - self.start_at).total_seconds() // 60)

    @property
    def total_display(self) -> str:
        return format_ghs(self.total_pesewas)

    def save(self, *args, **kwargs):
        # A 6-hex reference has 16.7M values; a collision is rare but the
        # unique index makes it a crash rather than a duplicate. Retry a few
        # times with a fresh reference before giving up.
        for attempt in range(5):
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError as exc:
                if ('reference' in constraint_name(exc) and attempt < 4
                        and self._state.adding):
                    self.reference = generate_reference()
                    continue
                raise
