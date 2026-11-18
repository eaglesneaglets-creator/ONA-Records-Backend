"""
/api/v1/payments/mine/ — a customer's own payment history (C6).

GET  mine/                 every payment they have made or started, newest first
GET  mine/{id}/receipt/    the receipt PDF for one confirmed payment

Read-only, and only ever the requester's own rows: the queryset is filtered
by `customer=request.user`, so another person's payment id is a 404, never a
403 that would confirm it exists. Staff use /api/v1/admin/payments/.
"""

from __future__ import annotations

from django.db.models import IntegerField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import EntryType, LedgerEntry, Provider, Receipt, Transaction, TransactionStatus
from .money import format_ghs


class MyPaymentSerializer(serializers.ModelSerializer):
    booking = serializers.SerializerMethodField()
    how = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display')
    amount_display = serializers.CharField()
    fee_display = serializers.SerializerMethodField()
    charged_display = serializers.SerializerMethodField()
    refunded_pesewas = serializers.IntegerField(read_only=True)
    refunded_display = serializers.SerializerMethodField()
    receipt_number = serializers.CharField(source='receipt.number', default=None)

    class Meta:
        model = Transaction
        fields = ('id', 'created_at', 'booking', 'how', 'status', 'status_display',
                  'amount_pesewas', 'amount_display', 'fee_pesewas', 'fee_display',
                  'charged_display', 'refunded_pesewas', 'refunded_display', 'receipt_number')
        read_only_fields = fields

    def get_booking(self, obj) -> dict:
        b = obj.booking
        return {'id': str(b.pk), 'reference': b.reference, 'room': b.room.name,
                'start_at': b.start_at}

    def get_how(self, obj) -> str:
        """How the money arrived, in the customer's words."""
        if obj.provider == Provider.MANUAL:
            method = obj.get_method_display() if obj.method else 'Paid'
            return f'{method} · recorded by ONA'
        if obj.provider == Provider.CREDIT:
            return 'Deposit carried over'
        return 'Paid online (Paystack)'

    def get_fee_display(self, obj) -> str:
        return format_ghs(obj.fee_pesewas) if obj.fee_pesewas else ''

    def get_charged_display(self, obj) -> str:
        return format_ghs(obj.amount_pesewas + obj.fee_pesewas)

    def get_refunded_display(self, obj) -> str:
        return format_ghs(obj.refunded_pesewas) if obj.refunded_pesewas else ''


class MyPaymentViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = MyPaymentSerializer
    filter_backends = []
    http_method_names = ['get', 'head', 'options']

    def get_queryset(self):
        refunded = (LedgerEntry.objects
                    .filter(transaction=OuterRef('pk'), entry_type=EntryType.REFUND)
                    .order_by().values('transaction')
                    .annotate(t=Sum('amount_pesewas')).values('t')[:1])
        return (Transaction.objects
                .filter(customer=self.request.user)
                .select_related('booking__room', 'receipt')
                .annotate(refunded_pesewas=Coalesce(
                    Subquery(refunded, output_field=IntegerField()), Value(0)))
                .order_by('-created_at'))

    @action(detail=True, methods=['get'])
    def receipt(self, request, pk=None):
        from .pdf import render_receipt

        txn = self.get_object()
        rec = (Receipt.objects.filter(transaction=txn)
               .select_related('transaction__booking__customer', 'transaction__booking__room')
               .first())
        if rec is None or txn.status != TransactionStatus.SUCCEEDED:
            return Response({'detail': 'There is no receipt for this payment.'},
                            status=status.HTTP_404_NOT_FOUND)
        res = HttpResponse(render_receipt(rec), content_type='application/pdf')
        res['Content-Disposition'] = f'attachment; filename="ONA-receipt-{rec.number}.pdf"'
        res['Cache-Control'] = 'private, no-store'
        return res
