"""
Soft delete.

Adapted from the reference. It matters more here than it did there: this
platform holds money, and a booking or payment row is evidence. If a dispute
arrives three months after a cancelled session, "the record was deleted" is
not an acceptable answer — and Ghana's Data Protection Act sits alongside tax
and accounting obligations that require financial records to be retained.

So money-touching models never hard-delete. They are flagged, disappear from
normal queries, and remain available to admin and to an audit.

Note this is the opposite of a data-subject erasure request, which is a
deliberate, separate process — not something that should happen because a
user clicked "cancel booking".
"""

from django.db import models
from django.utils import timezone


class SoftDeleteManager(models.Manager):
    """Default manager: soft-deleted rows are invisible."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class AllObjectsManager(models.Manager):
    """Everything, deleted included. For admin and audit surfaces."""

    def deleted(self):
        return self.get_queryset().filter(deleted_at__isnull=False)


class SoftDeleteMixin(models.Model):
    """
    Flag rows as deleted rather than removing them.

        class Booking(SoftDeleteMixin, TimestampMixin, models.Model):
            ...

        booking.soft_delete(by=request.user)

        Booking.objects.all()       # live rows only
        Booking.all_objects.all()   # including deleted
        Booking.all_objects.deleted()

    IMPORTANT: declaring `objects` first makes it the default manager, which
    is what related lookups and the admin use. Reversing the order silently
    exposes deleted rows through every foreign key in the codebase.
    """

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deleted_by = models.ForeignKey(
        'accounts.User', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+',
        help_text='Who removed this. Null if the system did it.',
    )

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        abstract = True

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def soft_delete(self, by=None):
        """
        Mark as deleted. Idempotent — re-deleting keeps the original
        timestamp, so the record of when it actually happened survives.
        """
        if self.deleted_at is None:
            self.deleted_at = timezone.now()
            self.deleted_by = by
            self.save(update_fields=['deleted_at', 'deleted_by'])

    def restore(self):
        self.deleted_at = None
        self.deleted_by = None
        self.save(update_fields=['deleted_at', 'deleted_by'])
