"""Timestamp and primary-key mixins."""

import uuid

from django.db import models


class TimestampMixin(models.Model):
    """
    created_at / updated_at on every row.

    Worth having everywhere: "when did this booking change?" is one of the
    first questions asked when something goes wrong, and adding the column
    afterwards means a migration with no data for existing rows.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class UUIDPrimaryKeyMixin(models.Model):
    """
    UUID primary key instead of a sequential integer.

    Sequential ids leak volume — a booking reference of 47 tells a customer
    the studio has had 47 bookings, and lets anyone enumerate records by
    counting upward. This platform shows references publicly, so the keys
    must not be guessable.

    Human-readable identifiers (ONA-2481, Creative #028) are separate fields
    with their own rules; this is only the database key.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True
