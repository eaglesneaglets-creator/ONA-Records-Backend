"""
Shared models.

AuditLog is here rather than in accounts because it records actions from every
app — a payment release, a professional approval, a review takedown — and none
of those should have to depend on the accounts app to write a record.
"""

import uuid

from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """
    Who did what, when, and to what.

    This exists because of one screen: the admin payment release. That action
    moves money out of ONA's control and cannot be undone. When someone asks
    six months later why GHS 1,063 left the account, "the system released it"
    is not an answer. A named person, a timestamp, and the amount is.

    DESIGN NOTES

    actor is nullable and SET_NULL on delete. A deleted admin account must not
    take the record of their actions with it — that is exactly when the record
    matters most. actor_email is denormalised for the same reason: the FK may
    become null, but the string stays readable.

    target is a loose reference (app label + model + id) rather than a real
    GenericForeignKey. A hard reference would keep deleted rows alive and add
    a content-types join to every write; an audit trail should survive the
    thing it describes being deleted.

    Rows are never updated or deleted in application code. If that changes,
    it should be a deliberate, reviewed decision — an editable audit log is
    not an audit log.
    """

    class Action(models.TextChoices):
        # Money — the reason this model exists.
        PAYMENT_RELEASED = 'payment.released', 'Payment released to professional'
        PAYMENT_REFUNDED = 'payment.refunded', 'Payment refunded to customer'
        PAYMENT_HELD = 'payment.held', 'Payment received and held'

        # People
        PROFESSIONAL_APPROVED = 'professional.approved', 'Professional approved'
        PROFESSIONAL_REJECTED = 'professional.rejected', 'Professional rejected'
        USER_SUSPENDED = 'user.suspended', 'Account suspended'
        USER_REACTIVATED = 'user.reactivated', 'Account reactivated'

        # Identity masking — unmasking is a privileged act and is recorded.
        IDENTITIES_REVEALED = 'project.identities_revealed', 'Identities revealed to both parties'

        # Content
        REVIEW_PUBLISHED = 'review.published', 'Review published'
        REVIEW_REJECTED = 'review.rejected', 'Review rejected'

        # Bookings
        BOOKING_CANCELLED = 'booking.cancelled', 'Booking cancelled'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='audit_entries',
    )
    # Survives the actor being deleted.
    actor_email = models.EmailField(blank=True)

    action = models.CharField(max_length=64, choices=Action.choices, db_index=True)

    # Loose reference to whatever was acted on.
    target_type = models.CharField(max_length=64, blank=True, db_index=True)
    target_id = models.CharField(max_length=64, blank=True, db_index=True)

    # Free-form context: amounts, before/after values, a reason.
    # Never put credentials or full card details in here.
    metadata = models.JSONField(default=dict, blank=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'core_audit_log'
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=['action', '-created_at']),
            models.Index(fields=['target_type', 'target_id']),
        ]

    def __str__(self):
        return '%s by %s at %s' % (
            self.action, self.actor_email or 'system', self.created_at,
        )

    @classmethod
    def record(cls, action, actor=None, target=None, request=None, **metadata):
        """
        Write an entry.

            AuditLog.record(
                AuditLog.Action.PAYMENT_RELEASED,
                actor=request.user,
                target=payment,
                request=request,
                amount_pesewas=106300,
                to_professional='Creative #028',
            )

        Never raises. An audit write failing must not roll back the action it
        describes — losing the record is bad, but failing a payment release
        because logging broke is worse. Failures go to the logger instead.
        """
        import json
        import logging

        try:
            # Coerce metadata to something JSONField can store. Without this
            # an unserialisable value raises inside save() — at the driver
            # level, past any handling here — and takes the caller's
            # transaction with it. A payment release must not fail because
            # someone passed an odd object to the logger.
            try:
                json.dumps(metadata)
                safe_metadata = metadata
            except (TypeError, ValueError):
                safe_metadata = {k: repr(v) for k, v in metadata.items()}

            entry = cls(
                action=action,
                actor=actor if (actor and actor.is_authenticated) else None,
                actor_email=getattr(actor, 'email', '') or '',
                metadata=safe_metadata,
            )

            if target is not None:
                entry.target_type = target.__class__.__name__
                entry.target_id = str(getattr(target, 'pk', ''))

            if request is not None:
                # X-Forwarded-For is set by Railway's proxy; the first entry
                # is the client. Falls back to REMOTE_ADDR locally.
                forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
                entry.ip_address = (
                    forwarded.split(',')[0].strip() if forwarded
                    else request.META.get('REMOTE_ADDR')
                )

            entry.save()
            return entry
        except Exception:
            logging.getLogger(__name__).exception(
                'Audit write failed: action=%s actor=%s', action, getattr(actor, 'pk', None),
            )
            return None
