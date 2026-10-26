"""
/api/v1/admin/payments/ — staff view of every transaction (Task 14).

Read-only. Money moves only through apps/payments/ledger.py (provider
callbacks now; manual recording in Task 16), never through this endpoint.
Each row carries its ledger entries and the commission on any release, so
"what happened to this money?" is answered on one screen.
"""

from django.db.models import Prefetch
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.notifications.dispatch import notify_booking
from apps.notifications.models import Kind
from core.models import AuditLog

from core.permissions.roles import IsONAAdmin

from .models import EntryType, LedgerEntry, Provider, Transaction
from .money import format_ghs


class CommissionSerializer(serializers.Serializer):
    percent = serializers.IntegerField()
    amount_pesewas = serializers.IntegerField()
    amount_display = serializers.SerializerMethodField()

    def get_amount_display(self, obj) -> str:
        return format_ghs(obj.amount_pesewas)


class LedgerEntrySerializer(serializers.ModelSerializer):
    entry_type_display = serializers.CharField(source='get_entry_type_display')
    beneficiary_display = serializers.CharField(source='get_beneficiary_display')
    amount_display = serializers.SerializerMethodField()
    created_by_email = serializers.SerializerMethodField()
    commission = serializers.SerializerMethodField()
    # A provider callback leaves created_by empty; a person never does.
    recorded_by_staff = serializers.SerializerMethodField()

    class Meta:
        model = LedgerEntry
        fields = ('id', 'entry_type', 'entry_type_display', 'amount_pesewas', 'amount_display',
                  'beneficiary', 'beneficiary_display', 'created_at', 'created_by_email', 'note',
                  'commission', 'recorded_by_staff')
        read_only_fields = fields

    def get_amount_display(self, obj) -> str:
        return format_ghs(obj.amount_pesewas)

    def get_created_by_email(self, obj) -> str | None:
        return obj.created_by.email if obj.created_by_id else None

    def get_recorded_by_staff(self, obj) -> bool:
        return obj.created_by_id is not None

    def get_commission(self, obj) -> dict | None:
        c = getattr(obj, 'commission', None)
        return CommissionSerializer(c).data if c else None


class TransactionStaffSerializer(serializers.ModelSerializer):
    booking = serializers.SerializerMethodField()
    customer = serializers.SerializerMethodField()
    provider_display = serializers.CharField(source='get_provider_display')
    status_display = serializers.CharField(source='get_status_display')
    amount_display = serializers.CharField()
    ledger = LedgerEntrySerializer(source='ledger_entries', many=True)
    held_pesewas = serializers.SerializerMethodField()
    held_display = serializers.SerializerMethodField()
    is_manual = serializers.SerializerMethodField()
    method_display = serializers.CharField(source='get_method_display', read_only=True)
    commission_pesewas = serializers.SerializerMethodField()

    class Meta:
        model = Transaction
        fields = ('id', 'booking', 'customer', 'provider', 'provider_display', 'provider_ref',
                  'is_manual', 'method', 'method_display',
                  'amount_pesewas', 'amount_display', 'status', 'status_display',
                  'failure_reason', 'commission_percent', 'held_pesewas', 'held_display',
                  'commission_pesewas',
                  'created_at', 'ledger')
        read_only_fields = fields

    def get_booking(self, obj) -> dict:
        return {'id': str(obj.booking_id), 'reference': obj.booking.reference}

    def get_customer(self, obj) -> dict:
        c = obj.customer
        return {'id': str(c.pk), 'name': c.full_name or c.email, 'email': c.email}

    # Both totals are read from the prefetched entries: no query per row.
    def get_held_pesewas(self, obj) -> int:
        sign = {EntryType.HOLD: 1, EntryType.RELEASE: -1, EntryType.REFUND: -1}
        return sum(sign[e.entry_type] * e.amount_pesewas for e in obj.ledger_entries.all())

    def get_is_manual(self, obj) -> bool:
        return obj.provider == Provider.MANUAL

    def get_held_display(self, obj) -> str:
        return format_ghs(self.get_held_pesewas(obj))

    def get_commission_pesewas(self, obj) -> int:
        return sum(e.commission.amount_pesewas for e in obj.ledger_entries.all()
                   if getattr(e, 'commission', None))


def transactions_with_ledger():
    return Transaction.objects.select_related('booking', 'customer').prefetch_related(
        Prefetch('ledger_entries',
                 queryset=LedgerEntry.objects.select_related('commission', 'created_by')))


class TransactionStaffViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, IsONAAdmin]
    serializer_class = TransactionStaffSerializer
    filterset_fields = ('status', 'provider', 'booking', 'customer')
    search_fields = ('booking__reference', 'customer__email', 'customer__first_name',
                     'customer__last_name', 'provider_ref')
    ordering_fields = ('created_at', 'amount_pesewas')

    def get_queryset(self):
        return transactions_with_ledger()

    @action(detail=True, methods=['post'])
    def refund(self, request, pk=None):
        """POST /admin/payments/{id}/refund/ {amount_pesewas, note} — record money
        ONA has already returned (Task 16). Never more than is still held."""
        from .manual import LedgerError, record_refund

        txn = self.get_object()
        body = RefundSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            entry = record_refund(txn, body.validated_data['amount_pesewas'],
                                  body.validated_data['note'], actor=request.user)
        except LedgerError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        AuditLog.record(AuditLog.Action.PAYMENT_REFUNDED, actor=request.user, target=txn,
                        request=request, booking=txn.booking.reference, manual=True,
                        amount_pesewas=entry.amount_pesewas, note=entry.note,
                        provider=txn.provider)
        notify_booking(txn.booking, Kind.PAYMENT_REFUNDED)
        return Response(TransactionStaffSerializer(transactions_with_ledger().get(pk=txn.pk)).data)


class RefundSerializer(serializers.Serializer):
    amount_pesewas = serializers.IntegerField(min_value=1)
    note = serializers.CharField(max_length=255)
