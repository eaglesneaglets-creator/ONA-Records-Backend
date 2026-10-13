"""
A record of every notification the platform tries to send.

Two jobs:

  1. **Exactly once.** A notification that must not repeat (a reminder for
     a given session time, the confirmation of a given payment) carries a
     dedupe_key, unique in the database. The reminder sweep runs every 15
     minutes and a payment callback can be replayed; neither can send twice.
  2. **Answerable.** "Did the customer get told?" is a support question with
     a real answer here: what was sent, to whom, when, and if it failed, why.

Email is the only channel in v0.1. SMS (Hubtel, separate credentials) is a
second channel on the same log.
"""

from django.db import models

from core.mixins.timestamp import TimestampMixin, UUIDPrimaryKeyMixin


class Kind(models.TextChoices):
    BOOKING_RECEIVED = 'booking_received', 'Booking received'
    BOOKING_RESCHEDULED = 'booking_rescheduled', 'Booking rescheduled'
    BOOKING_CANCELLED = 'booking_cancelled', 'Booking cancelled'
    PAYMENT_CONFIRMED = 'payment_confirmed', 'Payment confirmed'
    REMINDER = 'reminder', 'Session reminder'
    VERIFY_EMAIL = 'verify_email', 'Verify email address'
    PASSWORD_RESET = 'password_reset', 'Password reset'


class Channel(models.TextChoices):
    EMAIL = 'email', 'Email'


class Status(models.TextChoices):
    SENDING = 'sending', 'Sending'
    SENT = 'sent', 'Sent'
    FAILED = 'failed', 'Failed'
    SKIPPED = 'skipped', 'Skipped'   # e.g. booking cancelled before its reminder ran


class NotificationLog(UUIDPrimaryKeyMixin, TimestampMixin):
    kind = models.CharField(max_length=24, choices=Kind.choices, db_index=True)
    channel = models.CharField(max_length=8, choices=Channel.choices, default=Channel.EMAIL)
    recipient = models.CharField(max_length=254)
    booking = models.ForeignKey('studio.StudioBooking', null=True, blank=True,
                                on_delete=models.SET_NULL, related_name='notifications')
    dedupe_key = models.CharField(
        max_length=160, null=True, blank=True, unique=True,
        help_text='Set for notifications that must never repeat. Null for ones that may '
                  '(a second password-reset request is a second email).',
    )
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.SENDING)
    attempts = models.PositiveSmallIntegerField(default=1)
    sent_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self) -> str:
        return f'{self.get_kind_display()} → {self.recipient} · {self.status}'
