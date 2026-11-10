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

from collections import defaultdict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Sum
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.notifications.dispatch import notify_booking
from apps.notifications.models import Kind
from apps.payments.models import (
    Commission,
    EntryType,
    LedgerEntry,
    PaymentMethod,
    Receipt,
    Transaction,
    TransactionStatus,
)
from apps.payments.money import format_ghs
from apps.payments.staff import TransactionStaffSerializer, transactions_with_ledger
from core.models import AuditLog
from core.permissions.roles import IsONAAdmin

from .brokering import (
    AssignmentRefused,
    AssignProfessionalSerializer,
    StaffBookingCreateSerializer,
    assign_professional,
)
from .models import SLOT_HOLDING_STATUSES, BookingStatus, StudioBooking, constraint_name
from .money import with_paid
from .serializers import BookingSerializer
from .views import SlotTaken

TZ = ZoneInfo(settings.TIME_ZONE)


def staff_bookings():
    return with_paid(
        StudioBooking.objects
        .select_related('customer', 'room', 'engineer', 'service', 'assigned_professional',
                        'created_by')
        .annotate(has_receipt=Exists(Receipt.objects.filter(transaction__booking=OuterRef('pk'))),
                  # No booking of theirs was made before this one: a first visit.
                  is_first_booking=~Exists(StudioBooking.all_objects.filter(
                      customer=OuterRef('customer'), created_at__lt=OuterRef('created_at')))))


def customer_brief(u) -> dict:
    from apps.accounts.journey import INTENTS
    return {'id': str(u.pk), 'public_ref': u.public_ref, 'name': u.full_name or u.email,
            'email': u.email, 'phone': u.phone,
            'intent_display': INTENTS.get(u.intent, '')}


class StaffBookingSerializer(BookingSerializer):
    customer = serializers.SerializerMethodField()
    paid_pesewas = serializers.IntegerField(read_only=True)
    refunded_pesewas = serializers.IntegerField(read_only=True)
    paid_display = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    allowed_actions = serializers.SerializerMethodField()
    assigned_professional = serializers.SerializerMethodField()
    created_by_email = serializers.SerializerMethodField()
    room_total_display = serializers.SerializerMethodField()
    outstanding_pesewas = serializers.SerializerMethodField()
    outstanding_display = serializers.SerializerMethodField()
    is_first_booking = serializers.BooleanField(read_only=True, default=False)

    class Meta(BookingSerializer.Meta):
        fields = BookingSerializer.Meta.fields + (
            'customer', 'paid_pesewas', 'refunded_pesewas', 'paid_display', 'status_display',
            'allowed_actions', 'updated_at', 'assigned_professional', 'created_by_email',
            'room_total_display', 'outstanding_pesewas', 'outstanding_display',
            'is_first_booking')
        read_only_fields = fields

    def get_assigned_professional(self, obj) -> dict | None:
        p = obj.assigned_professional
        return {'id': str(p.pk), 'name': p.name, 'stage_name': p.stage_name} if p else None

    def get_created_by_email(self, obj) -> str | None:
        return obj.created_by.email if obj.created_by_id else None

    def get_room_total_display(self, obj) -> str:
        return format_ghs(obj.room_total_pesewas)

    # Still owed, from the annotated totals — what the "Record payment" form
    # offers, so the browser never subtracts money (Task 16).
    def get_outstanding_pesewas(self, obj) -> int:
        return max(obj.total_pesewas - getattr(obj, 'paid_pesewas', 0)
                   + getattr(obj, 'refunded_pesewas', 0), 0)

    def get_outstanding_display(self, obj) -> str:
        return format_ghs(self.get_outstanding_pesewas(obj))

    def get_customer(self, obj) -> dict:
        return customer_brief(obj.customer)

    def get_paid_display(self, obj) -> str:
        return format_ghs(getattr(obj, 'paid_pesewas', 0) - getattr(obj, 'refunded_pesewas', 0))

    def get_allowed_actions(self, obj) -> list[str]:
        return [name for name, rule in TRANSITIONS.items() if rule.allows(obj)]


class StaffBookingDetailSerializer(StaffBookingSerializer):
    transactions = serializers.SerializerMethodField()
    history = serializers.SerializerMethodField()
    available_credits = serializers.SerializerMethodField()

    class Meta(StaffBookingSerializer.Meta):
        fields = StaffBookingSerializer.Meta.fields + ('transactions', 'history',
                                                       'available_credits')
        read_only_fields = fields

    def get_available_credits(self, obj) -> list:
        """The customer's unexpired deposit credit — offered only on a booking
        that can take it (upcoming, holding its slot, something owed)."""
        from apps.payments.credit import available_credits
        from apps.payments.manual import outstanding
        if obj.status not in SLOT_HOLDING_STATUSES or obj.end_at <= timezone.now() \
                or outstanding(obj) <= 0:
            return []
        return [c.as_dict() for c in available_credits(obj.customer)
                if c.booking_id != str(obj.pk)]

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


class ManualPaymentSerializer(serializers.Serializer):
    amount_pesewas = serializers.IntegerField(min_value=1, max_value=100_000_000)
    method = serializers.ChoiceField(choices=PaymentMethod.choices)
    reference = serializers.CharField(max_length=100, required=False, allow_blank=True)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True)


class TransitionSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)
    # Cancel only: ONA's terms — with 48 hours' notice the deposit becomes
    # credit for a session within 30 days. Staff decide (e.g. not for a no-show).
    keep_as_credit = serializers.BooleanField(required=False, default=False)


class ApplyCreditSerializer(serializers.Serializer):
    source_booking = serializers.UUIDField()


def _credit_fields(name, body, held) -> dict:
    """On a staff cancel with keep_as_credit, record the credit window the way
    a customer's own cancellation does (apps/payments/credit.py reads it)."""
    from datetime import timedelta

    from django.conf import settings
    if name != 'cancel' or not body.get('keep_as_credit') or held <= 0:
        return {}
    until = timezone.localdate() + timedelta(days=settings.DEPOSIT_CREDIT_DAYS)
    return {'credit_pesewas': held, 'credit_until': until.isoformat()}


class StaffBookingViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET  /admin/bookings/?when=upcoming|past|today&status=&customer=&search=
    POST /admin/bookings/                    book for a customer (Task 15)
    GET  /admin/bookings/{id}/               with payments and history
    POST /admin/bookings/{id}/confirm|complete|cancel/   {reason?}
    POST /admin/bookings/{id}/assign/        {professional, professional_fee_pesewas}
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
                paid_pesewas=paid.paid_pesewas - paid.refunded_pesewas,
                **_credit_fields(name, body.validated_data, paid.paid_pesewas - paid.refunded_pesewas))
            notify_booking(booking, rule.notify)

        return Response(StaffBookingDetailSerializer(
            staff_bookings().get(pk=booking.pk), context=self.get_serializer_context()).data)

    def create(self, request):
        ser = StaffBookingCreateSerializer(data=request.data, context={'request': request})
        ser.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                booking = ser.save()
        except IntegrityError as exc:
            # Validation passed a moment ago; the exclusion constraint refused
            # it. Staff get the same honest answer as customers.
            name = constraint_name(exc)
            if not name.endswith(('_room_overlap', '_engineer_overlap')):
                raise
            which = 'engineer' if name.endswith('_engineer_overlap') else 'room'
            raise SlotTaken(f'That {which} was booked by someone else just now. '
                            'Please pick another time.') from exc
        return Response(StaffBookingDetailSerializer(
            staff_bookings().get(pk=booking.pk), context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def assign(self, request, pk=None):
        self.get_object()
        body = AssignProfessionalSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            assign_professional(pk, body.validated_data['professional'],
                                body.validated_data.get('professional_fee_pesewas', 0),
                                actor=request.user, request=request)
        except AssignmentRefused as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(StaffBookingDetailSerializer(
            staff_bookings().get(pk=pk), context=self.get_serializer_context()).data)

    @action(detail=True, methods=['post'], url_path='payments')
    def record_payment(self, request, pk=None):
        """POST /admin/bookings/{id}/payments/ {amount_pesewas, method, reference?, note?}
        — money ONA received off-platform (Task 16)."""
        from apps.payments.manual import LedgerError, record_payment

        booking = self.get_object()
        body = ManualPaymentSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        d = body.validated_data
        try:
            txn = record_payment(booking, d['amount_pesewas'], d['method'],
                                 d.get('reference', ''), d.get('note', ''), actor=request.user)
        except LedgerError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        AuditLog.record(AuditLog.Action.PAYMENT_HELD, actor=request.user, target=txn,
                        request=request, booking=booking.reference, manual=True,
                        amount_pesewas=txn.amount_pesewas, method=txn.method,
                        reference=txn.provider_ref or '')
        return Response(StaffBookingDetailSerializer(
            staff_bookings().get(pk=booking.pk), context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='apply-credit')
    def apply_credit(self, request, pk=None):
        """POST /admin/bookings/{id}/apply-credit/ {source_booking} — move a
        cancelled booking's deposit credit onto this one."""
        from apps.payments.credit import apply_credit
        from apps.payments.ledger import LedgerError

        target = self.get_object()
        body = ApplyCreditSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        source = StudioBooking.objects.filter(pk=body.validated_data['source_booking']).first()
        if source is None:
            return Response({'detail': 'That booking does not exist.'},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            apply_credit(source, target, actor=request.user, request=request)
        except LedgerError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(StaffBookingDetailSerializer(
            staff_bookings().get(pk=target.pk), context=self.get_serializer_context()).data)

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


def _new_customers() -> list:
    """Customers who joined in the last two weeks, newest first, with where
    each one has got to — so staff can welcome them or nudge them along."""
    from apps.accounts.journey import INTENTS, NEW_FOR, STAGES, stage
    rows = (User.objects.filter(role='customer', is_active=True,
                                date_joined__gte=timezone.now() - NEW_FOR)
            .order_by('-date_joined')[:6])
    out = []
    for u in rows:
        s = stage(u)
        out.append({'id': str(u.pk), 'name': u.full_name or u.email, 'email': u.email,
                    'date_joined': u.date_joined, 'stage': s, 'stage_display': STAGES[s],
                    'intent_display': INTENTS.get(u.intent, '')})
    return out


def _funnel() -> dict:
    """Customers who joined in the last 30 days, by how far they have got."""
    from apps.accounts.journey import STAGES, stage
    joined = User.objects.filter(role='customer', is_active=True,
                                 date_joined__gte=timezone.now() - timedelta(days=30))
    counts = dict.fromkeys(STAGES, 0)
    for u in joined[:500]:
        counts[stage(u)] += 1
    # Cumulative: everyone who paid also booked.
    order = list(STAGES)
    reached = {k: sum(counts[o] for o in order[i:]) for i, k in enumerate(order)}
    return {'joined_30d': joined.count(),
            'stages': [{'key': k, 'label': STAGES[k], 'reached': reached[k]} for k in order]}


class StaffSetupView(APIView):
    """
    GET /admin/setup/ — "Launch ONA": a fixed checklist of what has to be true
    before real bookings, each item ticked by manage.py launch_check's own
    checks (so the console and the command can never disagree) and pointing
    at where it is fixed.
    """

    permission_classes = [IsAuthenticated, IsONAAdmin]

    # (key, group, title, needles in launch_check messages, when done, href, where)
    CHECKS = (
        ('catalogue', 'Your studio', "Put ONA's real rooms, engineers and services on sale",
         ('on sale', 'placeholder text on'),
         'Studio One, your engineer and services are live, with real wording.',
         '/admin/catalogue', 'Studio & prices'),
        ('faq', 'Your studio', 'Check the answers customers read',
         ('in the faq',), 'No placeholder answers are left in the questions page.',
         '/admin/content', 'Questions (FAQ)'),
        ('paystack', 'Getting paid', 'Connect Paystack for Mobile Money',
         ('paystack_secret_key',), 'Online payment is connected with a live key.', None, 'Railway settings'),
        ('invoice_contact', 'Invoices & receipts', "Add the studio manager's contact",
         ('invoice_contact',), 'Invoices show who to call.', None, 'Railway settings'),
        ('invoice_bank', 'Invoices & receipts', 'Add the bank account for transfers',
         ('invoice_bank_details',), 'Invoices show where to transfer.', None, 'Railway settings'),
        ('invoice_tin', 'Invoices & receipts', "Add ONA's TIN",
         ('invoice_tin',), 'Invoices carry the TIN.', None, 'Railway settings'),
        ('email', 'Talking to customers', 'Send real emails',
         ('email is not sent',), 'Confirmations, receipts and reminders reach customers.', None,
         'Railway settings'),
        ('verified', 'Talking to customers', 'Ask for a confirmed email before booking',
         ('booking_requires_verified_email',), 'Bookings need a confirmed email.', None,
         'Railway settings'),
        ('worker', 'Running day to day', 'Start the background worker',
         ('celery broker',), 'Reminders, the payment check and the 1-hour release run on time.',
         None, 'Railway settings'),
        ('safety', 'Running day to day', 'Lock down the production settings',
         ('debug is on', 'admin_url', 'studio_commission_percent'),
         'Debug is off, the Django admin is at a private path.', None, 'Railway settings'),
    )

    @staticmethod
    def _summary(messages):
        """launch_check says one line per placeholder row; a checklist step
        wants one sentence. 'Placeholder text on room "X" - …' lines collapse
        to 'Still placeholder: rooms X, Y; services Z.'"""
        import re
        named = defaultdict(list)
        rest = []
        for m in messages:
            hit = re.match(r'Placeholder text on (\w+) "([^"]+)"', m)
            if hit:
                named[hit.group(1)].append(hit.group(2))
            else:
                rest.append(m)
        if not named:
            return ' '.join(rest)
        parts = '; '.join(f'{kind}s {", ".join(names)}' if len(names) > 1 else f'{kind} {names[0]}'
                          for kind, names in named.items())
        return ' '.join([f'Still placeholder text: {parts}. Replace each one, or switch it off.', *rest])

    def get(self, request):
        from apps.studio.management.commands.launch_check import collect

        found = [(level, message) for level, message in collect()]
        items = []
        for key, group, title, needles, ok_text, href, where in self.CHECKS:
            hits = [(lv, m) for lv, m in found if any(n in m.lower() for n in needles)]
            items.append({
                'key': key, 'group': group, 'title': title, 'done': not hits,
                'level': ('blocker' if any(lv == 'BLOCKER' for lv, _ in hits) else 'warn')
                if hits else 'ok',
                'body': self._summary([m for _, m in hits]) if hits else ok_text,
                'href': href, 'where': where,
            })
        blockers = sum(1 for i in items if i['level'] == 'blocker')
        return Response({'ready': blockers == 0, 'blockers': blockers,
                         'done': sum(1 for i in items if i['done']), 'total': len(items),
                         'items': items})


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
            'new_customers': _new_customers(),
            'funnel': _funnel(),
            'today': StaffBookingSerializer(today, many=True, context=ctx).data,
        })

