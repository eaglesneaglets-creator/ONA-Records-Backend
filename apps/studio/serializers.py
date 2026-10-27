"""
Studio serializers.

CATALOGUE — read-only and deliberately flat. Each money field ships twice:
the integer pesewas the frontend does arithmetic with, and the formatted GHS
string it displays — so no client ever re-implements format_ghs and drifts
from the backend on rounding or thousands separators. Nothing internal
leaks: no deleted_at/deleted_by, no sort_order.

BOOKINGS — see BookingCreateSerializer for the pricing rule.
"""

from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from .availability import CLOSE_HOUR, OPEN_HOUR, STUDIO_TZ, is_free
from .models import BookingStatus, Engineer, Package, Room, Service, StudioBooking


class RoomSerializer(serializers.ModelSerializer):
    hourly_rate_display = serializers.CharField(read_only=True)

    class Meta:
        model = Room
        fields = ('id', 'name', 'description', 'hourly_rate_pesewas', 'hourly_rate_display',
                  'capacity')
        read_only_fields = fields


class EngineerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Engineer
        fields = ('id', 'name', 'bio', 'specialties')
        read_only_fields = fields


class PackageSerializer(serializers.ModelSerializer):
    price_display = serializers.CharField(read_only=True)
    room_name = serializers.SerializerMethodField()

    class Meta:
        model = Package
        fields = ('id', 'name', 'description', 'hours', 'price_pesewas', 'price_display',
                  'room', 'room_name', 'includes')
        read_only_fields = fields

    def get_room_name(self, obj) -> str | None:
        return obj.room.name if obj.room_id else None


class ServiceSerializer(serializers.ModelSerializer):
    base_price_display = serializers.CharField(read_only=True)

    class Meta:
        model = Service
        fields = ('id', 'name', 'description', 'base_price_pesewas', 'base_price_display',
                  'duration_minutes')
        read_only_fields = fields


class SlotSerializer(serializers.Serializer):
    start_at = serializers.DateTimeField()
    end_at = serializers.DateTimeField()
    state = serializers.ChoiceField(
        choices=['available', 'booked', 'maintenance', 'holiday', 'past'])


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------

MIN_HOURS, MAX_HOURS = 1, CLOSE_HOUR - OPEN_HOUR

# Slot-squatting defence while bookings hold a slot without payment (until
# Phase 2 adds payment and expiry of stale holds). A customer may hold at
# most this many pending bookings, no further ahead than this.
MAX_OPEN_PENDING = 3
MAX_DAYS_AHEAD = 90


def check_start(value):
    """A session start the studio can actually offer. Shared by create and
    reschedule so the two can never disagree about what is bookable."""
    local = value.astimezone(STUDIO_TZ)
    if local.minute or local.second or local.microsecond:
        raise serializers.ValidationError('Sessions start on the hour.')
    if value <= timezone.now():
        raise serializers.ValidationError('That time has already passed.')
    if value > timezone.now() + timedelta(days=MAX_DAYS_AHEAD):
        raise serializers.ValidationError(f'Bookings open up to {MAX_DAYS_AHEAD} days ahead.')
    if not OPEN_HOUR <= local.hour < CLOSE_HOUR:
        raise serializers.ValidationError(
            f'The studio is open {OPEN_HOUR:02d}:00 to {CLOSE_HOUR:02d}:00.')
    return value


def check_closing(start, hours):
    """End time for a session, refusing one that runs past closing."""
    end = start + timedelta(hours=hours)
    closes = start.astimezone(STUDIO_TZ).replace(hour=CLOSE_HOUR, minute=0, second=0,
                                                  microsecond=0)
    if end.astimezone(STUDIO_TZ) > closes:
        raise serializers.ValidationError(
            {'hours': f'That runs past closing time ({CLOSE_HOUR:02d}:00).'})
    return end


class BookingSerializer(serializers.ModelSerializer):
    """What a customer sees of their booking. Catalogue rows are nested
    read-only so the dashboard needs no second request."""

    room = RoomSerializer(read_only=True)
    engineer = EngineerSerializer(read_only=True)
    service = ServiceSerializer(read_only=True)
    total_display = serializers.CharField(read_only=True)
    duration_minutes = serializers.IntegerField(read_only=True)
    can_change = serializers.BooleanField(read_only=True)
    has_receipt = serializers.SerializerMethodField()
    # Manual brokering (Task 15). The public view only: a display name and
    # skills, never contact details (PRODUCT.md constraint 1).
    professional = serializers.SerializerMethodField()
    professional_fee_display = serializers.SerializerMethodField()
    # Net of refunds, so the customer is told the truth before cancelling a
    # booking they have paid for (QA F1).
    paid_pesewas = serializers.SerializerMethodField()
    paid_display = serializers.SerializerMethodField()

    def get_paid_pesewas(self, obj) -> int:
        from .money import net_paid
        return net_paid(obj)

    def get_paid_display(self, obj) -> str:
        from apps.payments.money import format_ghs
        return format_ghs(self.get_paid_pesewas(obj))

    outstanding_pesewas = serializers.SerializerMethodField()
    outstanding_display = serializers.SerializerMethodField()
    # Whether to show "Pay now": Paystack configured, the booking still holds
    # its slot, has not ended, and something is owed (ADR-018).
    can_pay_online = serializers.SerializerMethodField()

    def get_outstanding_pesewas(self, obj) -> int:
        return max(obj.total_pesewas - self.get_paid_pesewas(obj), 0)

    def get_outstanding_display(self, obj) -> str:
        from apps.payments.money import format_ghs
        return format_ghs(self.get_outstanding_pesewas(obj))

    def get_can_pay_online(self, obj) -> bool:
        from django.utils import timezone

        from apps.payments import paystack
        return (paystack.is_configured()
                and obj.status in (BookingStatus.PENDING, BookingStatus.CONFIRMED)
                and obj.end_at > timezone.now() and self.get_outstanding_pesewas(obj) > 0)

    def get_professional(self, obj) -> dict | None:
        from apps.marketplace.serializers import ProfessionalPublicSerializer
        pro = obj.assigned_professional
        return ProfessionalPublicSerializer(pro).data if pro else None

    def get_professional_fee_display(self, obj) -> str:
        from apps.payments.money import format_ghs
        return format_ghs(obj.professional_fee_pesewas)

    def get_has_receipt(self, obj) -> bool:
        annotated = getattr(obj, 'has_receipt', None)  # list views annotate it
        if annotated is not None:
            return bool(annotated)
        return obj.transactions.filter(receipt__isnull=False).exists()

    class Meta:
        model = StudioBooking
        fields = ('id', 'reference', 'status', 'room', 'engineer', 'service',
                  'start_at', 'end_at', 'duration_minutes', 'total_pesewas', 'total_display',
                  'notes', 'created_at', 'can_change', 'has_receipt',
                  'professional', 'professional_fee_pesewas', 'professional_fee_display',
                  'paid_pesewas', 'paid_display', 'outstanding_pesewas', 'outstanding_display',
                  'can_pay_online')
        read_only_fields = fields


class BookingCreateSerializer(serializers.Serializer):
    """
    POST /studio/bookings/

    PRICE: room hourly rate × hours, frozen onto the booking. The service
    chosen is what the session is for; its base price is informational in
    v0.1. The "service fee" line in the approved design is ONA's commission,
    which is still an open question (spec OQ3) and lands with Phase 2.

    Validation gives the customer a sentence. The exclusion constraint gives
    the guarantee — see views.BookingViewSet.create for the race.
    """

    room = serializers.PrimaryKeyRelatedField(queryset=Room.objects.active())
    engineer = serializers.PrimaryKeyRelatedField(queryset=Engineer.objects.active())
    service = serializers.PrimaryKeyRelatedField(queryset=Service.objects.active())
    start_at = serializers.DateTimeField()
    hours = serializers.IntegerField(min_value=MIN_HOURS, max_value=MAX_HOURS)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=1000)

    def validate_start_at(self, value):
        return check_start(value)

    def validate(self, attrs):
        start = attrs['start_at']
        end = check_closing(start, attrs['hours'])

        customer = self.context['request'].user
        open_pending = StudioBooking.objects.filter(
            customer=customer, status=BookingStatus.PENDING, end_at__gt=timezone.now(),
        ).count()
        if open_pending >= MAX_OPEN_PENDING:
            raise serializers.ValidationError(
                f'You already have {open_pending} bookings waiting for confirmation. '
                'ONA will confirm or release them before you can add more.')

        ok, why = is_free(room=attrs['room'], engineer=attrs['engineer'],
                          start_at=start, end_at=end)
        if not ok:
            raise serializers.ValidationError({'start_at': why})

        attrs['end_at'] = end
        attrs['total_pesewas'] = attrs['room'].hourly_rate_pesewas * attrs['hours']
        return attrs

    def create(self, validated):
        from apps.payments.documents import issue_invoice

        validated.pop('hours')  # folded into end_at
        customer = self.context['request'].user
        # The cap re-checked under a lock on the customer's row (QA F3):
        # parallel requests all passed the unlocked check in validate().
        # The view wraps save() in a transaction, so the lock holds until
        # this booking commits or rolls back.
        type(customer).objects.select_for_update().filter(pk=customer.pk).first()
        open_pending = StudioBooking.objects.filter(
            customer=customer, status=BookingStatus.PENDING, end_at__gt=timezone.now(),
        ).count()
        if open_pending >= MAX_OPEN_PENDING:
            raise serializers.ValidationError(
                f'You already have {open_pending} bookings waiting for confirmation. '
                'ONA will confirm or release them before you can add more.')
        booking = StudioBooking.objects.create(customer=customer, **validated)
        # Same transaction as the booking (the view wraps save() in atomic):
        # every booking has its invoice from the moment it exists.
        issue_invoice(booking)
        return booking


class BookingRescheduleSerializer(serializers.Serializer):
    """
    POST /studio/bookings/{id}/reschedule/  {"start_at": ...}

    Moves the session to a new start, keeping room, engineer and length — so
    the price, frozen at creation, does not change. A different room or
    length is a new booking. The booking's own old slot does not count as a
    clash; the exclusion constraint still guards the race.
    """

    start_at = serializers.DateTimeField()

    def validate_start_at(self, value):
        return check_start(value)

    def validate(self, attrs):
        booking = self.context['booking']
        if not booking.can_change:
            raise serializers.ValidationError(
                'Only upcoming pending or confirmed bookings can be moved.')
        hours = booking.duration_minutes // 60
        start = attrs['start_at']
        end = check_closing(start, hours)
        if start == booking.start_at:
            raise serializers.ValidationError({'start_at': 'That is already the booked time.'})
        ok, why = is_free(room=booking.room, engineer=booking.engineer,
                          start_at=start, end_at=end, exclude_booking=booking)
        if not ok:
            raise serializers.ValidationError({'start_at': why})
        attrs['end_at'] = end
        return attrs


class BookingSummarySerializer(serializers.Serializer):
    upcoming_count = serializers.IntegerField()
    pending_count = serializers.IntegerField()
    upcoming_hours = serializers.IntegerField()
    next = BookingSerializer(allow_null=True)
