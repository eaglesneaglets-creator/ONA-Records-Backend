"""
/api/v1/admin/customers/ — staff directory of customer accounts (Task 14).

Read-only in v0.1. Creating a customer on someone's behalf (walk-ins, phone
bookings) is Task 15. Staff see real names and contact details here: they
are ONA, and they need them to run the studio.
"""

from django.db.models import Count, IntegerField, Max, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import serializers, viewsets
from rest_framework.permissions import IsAuthenticated

from apps.payments.models import Transaction, TransactionStatus
from apps.payments.money import format_ghs
from core.permissions.roles import IsONAAdmin

from .models import User


class CustomerStaffSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    is_email_verified = serializers.BooleanField(read_only=True)
    booking_count = serializers.IntegerField(read_only=True)
    upcoming_count = serializers.IntegerField(read_only=True)
    last_session_at = serializers.DateTimeField(read_only=True)
    paid_pesewas = serializers.IntegerField(read_only=True)
    paid_display = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'public_ref', 'name', 'first_name', 'last_name', 'email', 'phone',
                  'is_active', 'is_email_verified', 'date_joined', 'booking_count',
                  'upcoming_count', 'last_session_at', 'paid_pesewas', 'paid_display')
        read_only_fields = fields

    def get_name(self, obj) -> str:
        return obj.full_name or obj.email

    def get_paid_display(self, obj) -> str:
        return format_ghs(obj.paid_pesewas)


class CustomerStaffViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, IsONAAdmin]
    serializer_class = CustomerStaffSerializer
    filterset_fields = ('is_active',)
    search_fields = ('email', 'first_name', 'last_name', 'phone')
    ordering_fields = ('date_joined', 'last_session_at', 'booking_count')
    ordering = ('-date_joined',)

    def get_queryset(self):
        live = Q(studio_bookings__deleted_at__isnull=True)
        paid = (Transaction.objects
                .filter(customer=OuterRef('pk'), status=TransactionStatus.SUCCEEDED)
                .order_by().values('customer').annotate(t=Sum('amount_pesewas')).values('t')[:1])
        return (User.objects.filter(role='customer')
                .annotate(
                    booking_count=Count('studio_bookings', filter=live),
                    upcoming_count=Count('studio_bookings', filter=live & Q(
                        studio_bookings__status__in=('pending', 'confirmed'),
                        studio_bookings__end_at__gt=timezone.now())),
                    last_session_at=Max('studio_bookings__start_at', filter=live),
                    paid_pesewas=Coalesce(Subquery(paid, output_field=IntegerField()), Value(0)),
                ))
