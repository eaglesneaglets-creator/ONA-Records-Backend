"""
ONA's copy of where each user's identity check stands.

The KYC service is the source of truth. This row is refreshed from it on
every call ONA makes and on every webhook, so a page or a staff list can say
"verified" without a network call — and so a later rule ("verified to book a
long session") has something fast to read.

No personal data is copied here: no name, no date of birth, no ID number,
no images. Status, the reviewer's reason (written for the person), dates.
"""

from django.conf import settings
from django.db import models
from django.utils.dateparse import parse_datetime


class IdentityStatus(models.TextChoices):
    DRAFT = 'draft', 'Not sent yet'
    SUBMITTED = 'submitted', 'Waiting for review'
    IN_REVIEW = 'in_review', 'Being reviewed'
    CHANGES_REQUESTED = 'changes_requested', 'Changes requested'
    APPROVED = 'approved', 'Verified'
    REJECTED = 'rejected', 'Not verified'


class IdentityCheck(models.Model):
    # The KYC service's id for this attempt.
    verification_id = models.UUIDField(primary_key=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='identity_checks')
    status = models.CharField(max_length=32, choices=IdentityStatus.choices)
    reason = models.TextField(blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user_id}: {self.status}'


def _dt(value):
    return parse_datetime(value) if value else None


def remember(user, v: dict) -> IdentityCheck:
    """Store what KYC just told us about one of this user's checks."""
    check, _ = IdentityCheck.objects.update_or_create(
        verification_id=v['id'],
        defaults={
            'user': user,
            'status': v['status'],
            'reason': v.get('decision_reason') or '',
            'submitted_at': _dt(v.get('submitted_at')),
            'decided_at': _dt(v.get('decided_at')),
        },
    )
    return check


def current_check(user) -> IdentityCheck | None:
    return user.identity_checks.order_by('-created_at').first()


def is_verified(user) -> bool:
    return user.identity_checks.filter(status=IdentityStatus.APPROVED).exists()


# Sent for review, or already decided in the person's favour: enough to book
# (settings.BOOKING_REQUIRES_IDENTITY_CHECK). Draft, changes requested and
# rejected are not — the person still has something to do.
SENT_STATUSES = (IdentityStatus.SUBMITTED, IdentityStatus.IN_REVIEW, IdentityStatus.APPROVED)


def has_sent_check(user) -> bool:
    """
    The latest check is with ONA or approved — or any earlier one was approved.

    The latest attempt decides, so a check sent back for changes blocks again
    until it is resent. An earlier approval still counts: a verified person
    stays verified.
    """
    latest = current_check(user)
    if latest is not None and latest.status in SENT_STATUSES:
        return True
    return is_verified(user)
