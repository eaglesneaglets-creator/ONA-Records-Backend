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

from django.core.validators import MinValueValidator
from django.db import models

from apps.payments.money import format_ghs
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

    name = models.CharField(max_length=120, unique=True)
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
