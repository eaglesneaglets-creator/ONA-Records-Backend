"""
Manual brokering — staff book on a customer's behalf and assign professionals
(Task 15, ADR-015: the admin runs the marketplace by hand in v0.1).

THE RULES, AND WHO THEY BIND

Staff are trusted operators, so the customer-facing guards against slot
squatting do not apply to them:

  - no cap on open pending bookings (MAX_OPEN_PENDING)
  - no 90-day horizon (MAX_DAYS_AHEAD)
  - outside 09:00–21:00 is allowed, but only when the request says so
    explicitly (`allow_outside_hours`) — the first attempt is refused with an
    `outside_hours` error the UI turns into a warning and a checkbox

Physics binds everyone (PRODUCT.md constraint 3): a start in the past, a
start off the hour, and any overlap on the room or engineer are refused for
staff exactly as for customers, and the exclusion constraint still has the
last word under concurrency.

The result is indistinguishable to the customer from a booking they made:
same model row, same invoice, same "booking received" email, same dashboard.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from apps.accounts.models import User
from apps.marketplace.models import Professional
from apps.notifications.dispatch import notify_account, notify_booking
from apps.notifications.models import Kind
from apps.payments.documents import DocumentError, has_payment, issue_invoice, revise_invoice
from core.models import AuditLog

from .availability import CLOSE_HOUR, OPEN_HOUR, STUDIO_TZ, is_free
from .models import BookingStatus, Engineer, Room, Service, StudioBooking

STAFF_MAX_HOURS = 12
MAX_FEE_PESEWAS = 100_000_000  # GHS 1,000,000 — a typo guard, not a business rule


def outside_hours(start, end) -> bool:
    s, e = start.astimezone(STUDIO_TZ), end.astimezone(STUDIO_TZ)
    closes = s.replace(hour=CLOSE_HOUR, minute=0, second=0, microsecond=0)
    return s.hour < OPEN_HOUR or e > closes


class NewCustomerSerializer(serializers.Serializer):
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=80)
    last_name = serializers.CharField(max_length=80, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                'An account with this email already exists. Choose them as the customer instead.')
        return value


class StaffBookingCreateSerializer(serializers.Serializer):
    """POST /admin/bookings/ — a booking made by staff for a customer."""

    customer = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(role='customer', is_active=True), required=False)
    new_customer = NewCustomerSerializer(required=False)

    room = serializers.PrimaryKeyRelatedField(queryset=Room.objects.active())
    engineer = serializers.PrimaryKeyRelatedField(queryset=Engineer.objects.active())
    service = serializers.PrimaryKeyRelatedField(queryset=Service.objects.active())
    start_at = serializers.DateTimeField()
    hours = serializers.IntegerField(min_value=1, max_value=STAFF_MAX_HOURS)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=1000)

    status = serializers.ChoiceField(
        choices=[BookingStatus.PENDING, BookingStatus.CONFIRMED], default=BookingStatus.PENDING)
    professional = serializers.PrimaryKeyRelatedField(
        queryset=Professional.objects.active(), required=False, allow_null=True)
    professional_fee_pesewas = serializers.IntegerField(
        min_value=0, max_value=MAX_FEE_PESEWAS, required=False, default=0)
    allow_outside_hours = serializers.BooleanField(required=False, default=False)

    def validate_start_at(self, value):
        local = value.astimezone(STUDIO_TZ)
        if local.minute or local.second or local.microsecond:
            raise serializers.ValidationError('Sessions start on the hour.')
        if value <= timezone.now():
            raise serializers.ValidationError('That time has already passed.')
        return value

    def validate(self, attrs):
        has_existing, has_new = 'customer' in attrs, 'new_customer' in attrs
        if has_existing == has_new:
            raise serializers.ValidationError(
                {'customer': 'Choose an existing customer or enter a new one — not both.'})

        fee, pro = attrs.get('professional_fee_pesewas', 0), attrs.get('professional')
        if fee and pro is None:
            raise serializers.ValidationError(
                {'professional_fee_pesewas': 'A fee needs a professional to go with it.'})

        start = attrs['start_at']
        end = start + timedelta(hours=attrs['hours'])
        if outside_hours(start, end) and not attrs.get('allow_outside_hours'):
            raise serializers.ValidationError({'outside_hours': (
                f'This session is outside opening hours ({OPEN_HOUR:02d}:00–{CLOSE_HOUR:02d}:00). '
                'Confirm you mean it, and make sure the studio will be staffed.')})

        ok, why = is_free(room=attrs['room'], engineer=attrs['engineer'],
                          start_at=start, end_at=end)
        if not ok:
            raise serializers.ValidationError({'start_at': why})

        attrs['end_at'] = end
        attrs['outside_hours'] = outside_hours(start, end)
        attrs['total_pesewas'] = attrs['room'].hourly_rate_pesewas * attrs['hours'] + fee
        return attrs

    def create(self, validated):
        staff = self.context['request'].user
        new = validated.pop('new_customer', None)
        created_customer = new is not None
        if created_customer:
            # No password: the customer chooses one from the welcome email.
            customer = User.objects.create_user(role='customer', password=None, **new)
        else:
            customer = validated.pop('customer')

        flagged = validated.pop('outside_hours')
        validated.pop('allow_outside_hours', None)
        validated.pop('hours')
        pro = validated.pop('professional', None)
        booking = StudioBooking.objects.create(
            customer=customer, created_by=staff, assigned_professional=pro, **validated)
        issue_invoice(booking)

        request = self.context['request']
        if created_customer:
            AuditLog.record('customer.created_by_staff', actor=staff, target=customer,
                            request=request, email=customer.email)
            notify_account(customer, Kind.ACCOUNT_CREATED)
        AuditLog.record(
            'booking.created_by_staff', actor=staff, target=booking, request=request,
            reference=booking.reference, customer=customer.email, status=booking.status,
            total_pesewas=booking.total_pesewas, outside_hours=flagged,
            professional=str(pro.pk) if pro else None,
            professional_fee_pesewas=booking.professional_fee_pesewas)
        notify_booking(booking, Kind.BOOKING_RECEIVED)
        return booking


class AssignProfessionalSerializer(serializers.Serializer):
    """POST /admin/bookings/{id}/assign/ — set, change or remove the professional."""

    professional = serializers.PrimaryKeyRelatedField(
        queryset=Professional.objects.active(), allow_null=True)
    professional_fee_pesewas = serializers.IntegerField(
        min_value=0, max_value=MAX_FEE_PESEWAS, required=False, default=0)

    def validate(self, attrs):
        if attrs['professional'] is None and attrs.get('professional_fee_pesewas'):
            raise serializers.ValidationError(
                {'professional_fee_pesewas': 'A fee needs a professional to go with it.'})
        return attrs


class AssignmentRefused(Exception):
    pass


def assign_professional(booking_pk, professional, fee: int, *, actor, request) -> StudioBooking:
    """
    Change who the booking includes and re-price it, under a row lock.

    The room's share of the total never changes here; only the fee part
    does. Refused for a booking that no longer holds its slot, and for one
    with a successful payment — what was paid for is frozen.
    """
    with transaction.atomic():
        # Lock the booking row only: Postgres cannot lock the nullable side
        # of the outer join to the professional, and need not.
        booking = (StudioBooking.objects.select_for_update(of=('self',))
                   .select_related('room', 'service', 'assigned_professional').get(pk=booking_pk))
        if not booking.holds_slot:
            raise AssignmentRefused('Only a pending or confirmed booking can be changed.')
        if has_payment(booking):
            raise AssignmentRefused(
                'This booking has been paid for, so what it includes can no longer change. '
                'Record anything extra as a separate job.')

        before = {'professional': str(booking.assigned_professional_id or ''),
                  'fee': booking.professional_fee_pesewas, 'total': booking.total_pesewas}
        room_total = booking.room_total_pesewas
        booking.assigned_professional = professional
        booking.professional_fee_pesewas = fee if professional else 0
        booking.total_pesewas = room_total + booking.professional_fee_pesewas
        if (str(booking.assigned_professional_id or ''), booking.professional_fee_pesewas) == (
                before['professional'], before['fee']):
            return booking  # nothing changed: no audit row, no email
        booking.save(update_fields=['assigned_professional', 'professional_fee_pesewas',
                                    'total_pesewas', 'updated_at'])
        try:
            revise_invoice(booking)
        except DocumentError as exc:  # a payment landed between the check and here
            raise AssignmentRefused(str(exc)) from exc

        AuditLog.record(
            'booking.professional_assigned', actor=actor, target=booking, request=request,
            reference=booking.reference, previous_professional=before['professional'] or None,
            professional=str(professional.pk) if professional else None,
            previous_fee_pesewas=before['fee'], fee_pesewas=booking.professional_fee_pesewas,
            previous_total_pesewas=before['total'], total_pesewas=booking.total_pesewas)
        notify_booking(booking, Kind.BOOKING_UPDATED)
    return booking
