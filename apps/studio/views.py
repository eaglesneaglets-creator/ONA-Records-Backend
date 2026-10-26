"""
Studio endpoints.

CATALOGUE (public, read-only)
    GET /api/v1/studio/rooms/        GET /api/v1/studio/rooms/{id}/
    GET /api/v1/studio/engineers/    GET /api/v1/studio/engineers/{id}/
    GET /api/v1/studio/services/     GET /api/v1/studio/services/{id}/
    GET /api/v1/studio/calendar/

AllowAny on purpose — the project default is IsAuthenticated, and a visitor
needs to see prices and free slots before deciding to register. Read-only:
staff edit the catalogue in admin (and the ONA admin dashboard in Phase 3).
The catalogue queryset is `.active()`, so an inactive or soft-deleted row is
a 404 here even by direct id.

BOOKINGS (customer)
    POST /api/v1/studio/bookings/     GET /api/v1/studio/bookings/[{id}/]
"""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.notifications.dispatch import notify_booking
from apps.notifications.models import Kind
from core.models import AuditLog
from core.permissions.roles import IsCustomer, IsOwnerOrONAAdmin, VerifiedEmailToBook

from .availability import MAX_RANGE_DAYS, availability
from .money import with_paid
from .models import (
    SLOT_HOLDING_STATUSES, BookingStatus, Engineer, Package, Room, Service, StudioBooking,
    constraint_name,
)
from .serializers import (
    BookingCreateSerializer, BookingRescheduleSerializer, BookingSerializer,
    BookingSummarySerializer, EngineerSerializer, PackageSerializer, RoomSerializer,
    ServiceSerializer,
    SlotSerializer,
)


class CatalogueViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'public'
    search_fields = ('name', 'description')
    ordering_fields = ('name',)

    def get_queryset(self):
        return self.model.objects.active()


class RoomViewSet(CatalogueViewSet):
    model = Room
    serializer_class = RoomSerializer


class EngineerViewSet(CatalogueViewSet):
    model = Engineer
    serializer_class = EngineerSerializer
    search_fields = ('name', 'bio')


class PackageViewSet(CatalogueViewSet):
    """GET /studio/packages/ — advertised bundles (Task 16)."""
    model = Package
    serializer_class = PackageSerializer

    def get_queryset(self):
        return super().get_queryset().select_related('room')


class ServiceViewSet(CatalogueViewSet):
    model = Service
    serializer_class = ServiceSerializer


class CalendarQuerySerializer(serializers.Serializer):
    """Validates room/engineer/start/end. Defaults to the next 14 days.
    `from` is a Python keyword, so the view maps from/to onto start/end."""

    room = serializers.PrimaryKeyRelatedField(queryset=Room.objects.active(), required=False)
    engineer = serializers.PrimaryKeyRelatedField(queryset=Engineer.objects.active(),
                                                  required=False)
    start = serializers.DateField(required=False)
    end = serializers.DateField(required=False)

    def validate(self, attrs):
        start = attrs.get('start') or timezone.localdate()
        end = attrs.get('end') or start + timedelta(days=13)
        if end < start:
            raise serializers.ValidationError({'to': 'Must not be before "from".'})
        if (end - start).days >= MAX_RANGE_DAYS:
            raise serializers.ValidationError({'to': f'At most {MAX_RANGE_DAYS} days at a time.'})
        attrs['start'], attrs['end'] = start, end
        return attrs


class CalendarView(APIView):
    """
    GET /api/v1/studio/calendar/?room=<id>&engineer=<id>&from=YYYY-MM-DD&to=YYYY-MM-DD

    Hour slots inside opening hours, each tagged available / booked /
    maintenance / holiday. Public: a visitor picks a date before registering.
    Never includes who booked a slot.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'public'

    @extend_schema(
        parameters=[
            OpenApiParameter('room', str, description='Room id'),
            OpenApiParameter('engineer', str, description='Engineer id'),
            OpenApiParameter('from', str, description='YYYY-MM-DD, default today'),
            OpenApiParameter('to', str, description='YYYY-MM-DD, default from+13'),
        ],
        responses=SlotSerializer(many=True),
    )
    def get(self, request):
        params = request.query_params
        data = {k: params[k] for k in ('room', 'engineer') if params.get(k)}
        if params.get('from'):
            data['start'] = params['from']
        if params.get('to'):
            data['end'] = params['to']
        query = CalendarQuerySerializer(data=data)
        query.is_valid(raise_exception=True)
        q = query.validated_data
        slots = availability(start=q['start'], end=q['end'],
                             room=q.get('room'), engineer=q.get('engineer'))
        return Response({
            'from': q['start'], 'to': q['end'],
            'room': str(q['room'].id) if q.get('room') else None,
            'engineer': str(q['engineer'].id) if q.get('engineer') else None,
            'slots': SlotSerializer([s.as_dict() for s in slots], many=True).data,
        })


class SlotTaken(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = 'slot_taken'
    default_detail = 'That time was booked by someone else just now. Please pick another.'


class BookingViewSet(mixins.CreateModelMixin, mixins.ListModelMixin,
                     mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    POST /studio/bookings/                    customer creates (no payment yet — Phase 2)
    GET  /studio/bookings/?when=upcoming|past own bookings; ONA staff see all
    GET  /studio/bookings/{id}/               own, or ONA staff
    GET  /studio/bookings/summary/            dashboard numbers for the caller
    POST /studio/bookings/{id}/cancel/        releases the slot
    POST /studio/bookings/{id}/reschedule/    new start, same room/engineer/length
    GET  /studio/bookings/{id}/invoice/       PDF (issued now if an older booking has none)
    GET  /studio/bookings/{id}/receipt/       PDF, 404 until a payment is confirmed

    Cancel and reschedule are recorded in AuditLog: they change what a
    customer has agreed to, and "who moved my session?" must be answerable.
    """

    permission_classes = [IsAuthenticated, IsOwnerOrONAAdmin]

    def get_permissions(self):
        if self.action == 'create':
            return [IsAuthenticated(), IsCustomer(), VerifiedEmailToBook()]
        return super().get_permissions()

    def get_throttles(self):
        # Creating holds a slot; reading your own list does not.
        if self.action == 'create':
            self.throttle_scope = 'booking'
            return [ScopedRateThrottle()]
        return super().get_throttles()

    def get_serializer_class(self):
        return BookingCreateSerializer if self.action == 'create' else BookingSerializer

    def get_queryset(self):
        from apps.payments.models import Receipt

        qs = with_paid(StudioBooking.objects).select_related(
            'room', 'engineer', 'service', 'assigned_professional').annotate(
            has_receipt=Exists(Receipt.objects.filter(transaction__booking=OuterRef('pk'))))
        if getattr(self, 'swagger_fake_view', False):  # schema generation
            return qs.none()
        u = self.request.user
        if not (u.role == 'admin' or u.is_superuser):
            qs = qs.filter(customer=u)

        when = self.request.query_params.get('when') if self.action == 'list' else None
        now = timezone.now()
        if when == 'upcoming':
            qs = qs.filter(status__in=SLOT_HOLDING_STATUSES, end_at__gt=now).order_by('start_at')
        elif when == 'past':
            qs = qs.exclude(status__in=SLOT_HOLDING_STATUSES, end_at__gt=now).order_by('-start_at')
        return qs

    @action(detail=False, methods=['get'])
    def summary(self, request):
        upcoming = list(
            with_paid(StudioBooking.objects.filter(
                customer=request.user, status__in=SLOT_HOLDING_STATUSES,
                end_at__gt=timezone.now(),
            )).select_related('room', 'engineer', 'service', 'assigned_professional').order_by('start_at')
        )
        data = {
            'upcoming_count': len(upcoming),
            'pending_count': sum(b.status == BookingStatus.PENDING for b in upcoming),
            'upcoming_hours': sum(b.duration_minutes for b in upcoming) // 60,
            'next': upcoming[0] if upcoming else None,
        }
        return Response(BookingSummarySerializer(data, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        booking = self.get_object()
        if not booking.can_change:
            return Response(
                {'detail': 'This booking can no longer be cancelled.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        before = booking.status
        booking.status = BookingStatus.CANCELLED
        booking.save(update_fields=['status', 'updated_at'])
        from .money import net_paid
        AuditLog.record('booking.cancelled', actor=request.user, target=booking, request=request,
                        reference=booking.reference, previous_status=before,
                        # Held money is not refunded by a cancel; staff follow up (QA F1).
                        paid_pesewas=net_paid(booking))
        notify_booking(booking, Kind.BOOKING_CANCELLED)
        return Response(BookingSerializer(booking, context=self.get_serializer_context()).data)

    @action(detail=True, methods=['post'])
    def reschedule(self, request, pk=None):
        booking = self.get_object()
        ser = BookingRescheduleSerializer(data=request.data,
                                          context={'request': request, 'booking': booking})
        ser.is_valid(raise_exception=True)
        old_start = booking.start_at
        booking.start_at = ser.validated_data['start_at']
        booking.end_at = ser.validated_data['end_at']
        try:
            with transaction.atomic():
                booking.save(update_fields=['start_at', 'end_at', 'updated_at'])
        except IntegrityError as exc:
            name = constraint_name(exc)
            if not name.endswith(('_room_overlap', '_engineer_overlap')):
                raise
            which = 'engineer' if name.endswith('_engineer_overlap') else 'room'
            raise SlotTaken(f'That {which} was booked by someone else just now. '
                            'Please pick another time.')
        AuditLog.record('booking.rescheduled', actor=request.user, target=booking,
                        request=request, reference=booking.reference,
                        from_start=old_start.isoformat(), to_start=booking.start_at.isoformat())
        notify_booking(booking, Kind.BOOKING_RESCHEDULED)
        return Response(BookingSerializer(booking, context=self.get_serializer_context()).data)

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                booking = ser.save()
        except IntegrityError as exc:
            # Validation passed a moment ago; another request won the race
            # and the exclusion constraint refused this one. Only the overlap
            # constraints mean that — anything else is a real error and must
            # not be reported to the customer as "slot taken".
            name = constraint_name(exc)
            if not name.endswith(('_room_overlap', '_engineer_overlap')):
                raise
            which = 'engineer' if name.endswith('_engineer_overlap') else 'room'
            raise SlotTaken(f'That {which} was booked by someone else just now. '
                            'Please pick another time.')
        notify_booking(booking, Kind.BOOKING_RECEIVED)
        return Response(BookingSerializer(booking, context=self.get_serializer_context()).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def invoice(self, request, pk=None):
        from apps.payments.documents import issue_invoice
        from apps.payments.pdf import render_invoice

        booking = self.get_object()
        inv = issue_invoice(booking)  # idempotent; covers bookings made before Task 10
        return _pdf(render_invoice(inv), f'ONA-invoice-{inv.number}.pdf')

    @action(detail=True, methods=['get'])
    def receipt(self, request, pk=None):
        from apps.payments.models import Receipt
        from apps.payments.pdf import render_receipt

        booking = self.get_object()
        rec = (Receipt.objects.filter(transaction__booking=booking)
               .select_related('transaction__booking__customer', 'transaction__booking__room')
               .order_by('-issued_at').first())
        if rec is None:
            return Response({'detail': 'No payment has been confirmed for this booking yet.'},
                            status=status.HTTP_404_NOT_FOUND)
        return _pdf(render_receipt(rec), f'ONA-receipt-{rec.number}.pdf')


def _pdf(content: bytes, filename: str) -> HttpResponse:
    res = HttpResponse(content, content_type='application/pdf')
    res['Content-Disposition'] = f'attachment; filename="{filename}"'
    # Personal financial documents: never cached by a shared proxy.
    res['Cache-Control'] = 'private, no-store'
    return res
