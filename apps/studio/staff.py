"""
/api/v1/admin/bookings/ and /api/v1/admin/overview/ — the staff console (Task 14).

Staff see every booking with its customer and what has been paid, and move
it through its lifecycle by hand:

    pending ──confirm──▶ confirmed ──complete──▶ completed
       └──────────────cancel──────────────┘

Every transition runs under a row lock (two staff clicking at once cannot
both act on a stale status), is written to AuditLog with the staff member as
actor, and emails the customer.

Creating bookings on a customer's behalf is Task 15.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, IntegerField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.notifications.dispatch import notify_booking
from apps.notifications.models import Kind
from apps.payments.models import Commission, EntryType, LedgerEntry, Receipt, Transaction, TransactionStatus
from apps.payments.money import format_ghs
from apps.payments.staff import TransactionStaffSerializer, transactions_with_ledger
from core.models import AuditLog
from core.permissions.roles import IsONAAdmin

from .models import SLOT_HOLDING_STATUSES, BookingStatus, StudioBooking
from .serializers import BookingSerializer

TZ = ZoneInfo(settings.TIME_ZONE)


def _sum(qs, field='amount_pesewas'):
    """A correlated SUM as a subquery, so several money totals can sit on one
    booking row without the join fan-out double-counting them."""
    return Coalesce(
        # order_by() drops the model's default ordering, which Postgres would
        # otherwise demand in the GROUP BY.
        Subquery(qs.order_by().values(_k=Value(1)).annotate(t=Sum(field)).values('t')[:1],
                 output_field=IntegerField()),
        Value(0),
    )


def staff_bookings():
    paid = Transaction.objects.filter(booking=OuterRef('pk'), status=TransactionStatus.SUCCEEDED)
    refunded = LedgerEntry.objects.filter(transaction__booking=OuterRef('pk'),
                                          entry_type=EntryType.REFUND)
    return (StudioBooking.objects
            .select_related('customer', 'room', 'engineer', 'service')
            .annotate(has_receipt=Exists(Receipt.objects.filter(transaction__booking=OuterRef('pk'))),
                      paid_pesewas=_sum(paid),
                      refunded_pesewas=_sum(refunded)))


def customer_brief(u) -> dict:
    return {'id': str(u.pk), 'public_ref': u.public_ref, 'name': u.full_name or u.email,
            'email': u.email, 'phone': u.phone}


class StaffBookingSerializer(BookingSerializer):
    customer = serializers.SerializerMethodField()
    paid_pesewas = serializers.IntegerField(read_only=True)
    refunded_pesewas = serializers.IntegerField(read_only=True)
    paid_display = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    allowed_actions = serializers.SerializerMethodField()

    class Meta(BookingSerializer.Meta):
        fields = BookingSerializer.Meta.fields + (
            'customer', 'paid_pesewas', 'refunded_pesewas', 'paid_display', 'status_display',
            'allowed_actions', 'updated_at')
        read_only_fields = fields

    def get_customer(self, obj) -> dict:
        return customer_brief(obj.customer)

    def get_paid_display(self, obj) -> str:
        return format_ghs(getattr(obj, 'paid_pesewas', 0) - getattr(obj, 'refunded_pesewas', 0))

    def get_allowed_actions(self, obj) -> list[str]:
        return [name for name, rule in TRANSITIONS.items() if rule.allows(obj)]


class StaffBookingDetailSerializer(StaffBookingSerializer):
    transactions = serializers.SerializerMethodField()
    history = serializers.SerializerMethodField()

    class Meta(StaffBookingSerializer.Meta):
        fields = StaffBookingSerializer.Meta.fields + ('transactions', 'history')
        read_only_fields = fields

    def get_transactions(self, obj) -> list:
        return TransactionStaffSerializer(
            transactions_with_ledger().filter(booking=obj), many=True).data

    def get_history(self, obj) -> list:
        rows = AuditLog.objects.filter(target_type='studio.studiobooking', target_id=str(obj.pk))
        return [{'action': r.action, 'actor_email': r.actor_email or None,
                 'created_at': r.created_at, 'metadata': r.metadata} for r in rows[:50]]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class Transition:
    def __init__(self, to, audit, notify, when, refusal):
        self.to, self.audit, self.notify = to, audit, notify
        self.when, self.refusal = when, refusal

    def allows(self, b) -> bool:
        return not b.is_deleted and self.when(b, timezone.now())


TRANSITIONS = {
    # Confirm before the session ends; a session that already happened is
    # completed (or cancelled), not confirmed after the fact.
    'confirm': Transition(
        BookingStatus.CONFIRMED, 'booking.confirmed', Kind.BOOKING_CONFIRMED,
        lambda b, now: b.status == BookingStatus.PENDING and b.end_at > now,
        'Only a pending booking that has not ended can be confirmed.'),
    # Complete once the session has started. Pending is allowed: a walk-in
    # paid at the desk may never have been confirmed in the system.
    'complete': Transition(
        BookingStatus.COMPLETED, 'booking.completed', Kind.BOOKING_COMPLETED,
        lambda b, now: b.status in SLOT_HOLDING_STATUSES and b.start_at <= now,
        'A booking can be marked completed once its session has started.'),
    # Staff may cancel any booking that still holds its slot — including a
    # no-show after the start, which the customer can no longer do.
    'cancel': Transition(
        BookingStatus.CANCELLED, 'booking.cancelled', Kind.BOOKING_CANCELLED,
        lambda b, now: b.status in SLOT_HOLDING_STATUSES,
        'Only a pending or confirmed booking can be cancelled.'),
}


class TransitionSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class StaffBookingViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET  /admin/bookings/?when=upcoming|past|today&status=&customer=&search=
    GET  /admin/bookings/{id}/               with payments and history
    POST /admin/bookings/{id}/confirm|complete|cancel/   {reason?}
    """

    permission_classes = [IsAuthenticated, IsONAAdmin]
    filterset_fields = ('status', 'customer', 'room', 'engineer')
    search_fields = ('reference', 'customer__email', 'customer__first_name',
                     'customer__last_name', 'customer__phone')
    ordering_fields = ('start_at', 'created_at', 'total_pesewas')

    def get_serializer_class(self):
        return StaffBookingDetailSerializer if self.action == 'retrieve' else StaffBookingSerializer

    def get_queryset(self):
        qs = staff_bookings()
        when = self.request.query_params.get('when') if self.action == 'list' else None
        now = timezone.now()
        if when == 'upcoming':
            qs = qs.filter(status__in=SLOT_HOLDING_STATUSES, end_at__gt=now).order_by('start_at')
        elif when == 'past':
            qs = qs.exclude(status__in=SLOT_HOLDING_STATUSES, end_at__gt=now).order_by('-start_at')
        elif when == 'today':
            start, end = today_bounds()
            qs = qs.filter(start_at__gte=start, start_at__lt=end).order_by('start_at')
        return qs

    def _transition(self, request, name):
        rule = TRANSITIONS[name]
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        self.get_object()  # 404 before locking, and object permissions

        with transaction.atomic():
            booking = StudioBooking.objects.select_for_update().get(pk=self.kwargs['pk'])
            if not rule.allows(booking):
                return Response({'detail': rule.refusal}, status=status.HTTP_400_BAD_REQUEST)
            before = booking.status
            booking.status = rule.to
            booking.save(update_fields=['status', 'updated_at'])
            paid = staff_bookings().get(pk=booking.pk)
            AuditLog.record(
                rule.audit, actor=request.user, target=booking, request=request,
                reference=booking.reference, previous_status=before, by_staff=True,
                reason=body.validated_data.get('reason', ''),
                # Cancelling a paid booking does not refund it — that is a
                # separate, deliberate ledger action (Task 16). Record what
                # was held so the follow-up is visible in the trail.
                paid_pesewas=paid.paid_pesewas - paid.refunded_pesewas)
            notify_booking(booking, rule.notify)

        return Response(StaffBookingDetailSerializer(
            staff_bookings().get(pk=booking.pk), context=self.get_serializer_context()).data)

    @action(detail=True, methods=['post'])
    def confirm(self, request, pk=None):
        return self._transition(request, 'confirm')

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        return self._transition(request, 'complete')

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        return self._transition(request, 'cancel')


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

def today_bounds():
    today = timezone.now().astimezone(TZ).date()
    start = datetime.combine(today, time.min, tzinfo=TZ)
    return start, start + timedelta(days=1)


class StaffOverviewView(APIView):
    """GET /admin/overview/ — the numbers on the console's first screen.
    Real counts only; every figure here is a query, never a placeholder."""

    permission_classes = [IsAuthenticated, IsONAAdmin]

    def get(self, request):
        now = timezone.now()
        day_start, day_end = today_bounds()
        month_ago = now - timedelta(days=30)
        holding = StudioBooking.objects.filter(status__in=SLOT_HOLDING_STATUSES)

        week = holding.filter(start_at__gte=now, start_at__lt=now + timedelta(days=7))
        week_minutes = sum((b.end_at - b.start_at).total_seconds() // 60
                           for b in week.only('start_at', 'end_at'))
        received = (Transaction.objects.filter(status=TransactionStatus.SUCCEEDED,
                                               created_at__gte=month_ago)
                    .aggregate(t=Sum('amount_pesewas'))['t'] or 0)
        refunded = (LedgerEntry.objects.filter(entry_type=EntryType.REFUND,
                                               created_at__gte=month_ago)
                    .aggregate(t=Sum('amount_pesewas'))['t'] or 0)
        commission = (Commission.objects.filter(created_at__gte=month_ago)
                      .aggregate(t=Sum('amount_pesewas'))['t'] or 0)
        customers = User.objects.filter(role='customer', is_active=True)

        ctx = {'request': request}
        pending = (staff_bookings().filter(status=BookingStatus.PENDING, end_at__gt=now)
                   .order_by('start_at'))
        today = (staff_bookings().filter(start_at__gte=day_start, start_at__lt=day_end)
                 .exclude(status=BookingStatus.CANCELLED).order_by('start_at'))
        return Response({
            'today_count': today.count(),
            'pending_count': pending.count(),
            'week_count': week.count(),
            'week_hours': int(week_minutes // 60),
            'customers_total': customers.count(),
            'customers_new_30d': customers.filter(date_joined__gte=month_ago).count(),
            'received_30d_pesewas': received - refunded,
            'received_30d_display': format_ghs(received - refunded),
            'commission_30d_pesewas': commission,
            'commission_30d_display': format_ghs(commission),
            'needs_confirmation': StaffBookingSerializer(pending[:5], many=True, context=ctx).data,
            'today': StaffBookingSerializer(today, many=True, context=ctx).data,
        })

