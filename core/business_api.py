"""
GET   /api/v1/content/rules/    the rules the public site and booking flow show (read-only)
GET   /api/v1/admin/business/   every business setting, as it is in force now
PATCH /api/v1/admin/business/   staff change some; validated, audited, live within seconds

The first save creates the BusinessSettings row from the environment values,
then applies the change, so untouched settings keep what they were.
"""

from __future__ import annotations

from rest_framework import serializers
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.business import FIELDS, from_environment, rules
from core.models import AuditLog, BusinessSettings
from core.permissions.roles import IsONAAdmin

PUBLIC_FIELDS = (
    'booking_deposit_percent', 'unpaid_hold_minutes', 'free_cancellation_hours',
    'deposit_credit_days', 'studio_open_hour', 'studio_close_hour',
    'pass_paystack_fee', 'paystack_fee_basis_points', 'tax_enabled', 'taxes',
)
MAX_TAXES = 6


class TaxLineSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=40)
    basis_points = serializers.IntegerField(min_value=1, max_value=5000,
                                            help_text='Hundredths of a percent: 1500 = 15%.')

    def validate_name(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Give the tax a name, e.g. VAT.')
        return value


class BusinessSettingsSerializer(serializers.Serializer):
    booking_deposit_percent = serializers.IntegerField(min_value=1, max_value=100)
    unpaid_hold_minutes = serializers.IntegerField(min_value=15, max_value=2880)
    free_cancellation_hours = serializers.IntegerField(min_value=0, max_value=720)
    deposit_credit_days = serializers.IntegerField(min_value=1, max_value=365)
    max_open_unpaid = serializers.IntegerField(min_value=1, max_value=20)
    reminder_hours_before = serializers.IntegerField(min_value=1, max_value=168)
    studio_open_hour = serializers.IntegerField(min_value=0, max_value=23)
    studio_close_hour = serializers.IntegerField(min_value=1, max_value=24)
    pass_paystack_fee = serializers.BooleanField()
    paystack_fee_basis_points = serializers.IntegerField(min_value=0, max_value=1000)
    studio_commission_percent = serializers.IntegerField(min_value=0, max_value=100)
    marketplace_commission_percent = serializers.IntegerField(min_value=0, max_value=100)
    tax_enabled = serializers.BooleanField()
    taxes = TaxLineSerializer(many=True)
    invoice_business_name = serializers.CharField(max_length=200)
    invoice_address = serializers.CharField(max_length=300, allow_blank=True)
    invoice_tin = serializers.CharField(max_length=40, allow_blank=True)
    invoice_contact = serializers.CharField(max_length=300, allow_blank=True)
    invoice_bank_details = serializers.CharField(max_length=300, allow_blank=True)

    def validate_taxes(self, value):
        if len(value) > MAX_TAXES:
            raise serializers.ValidationError(f'At most {MAX_TAXES} tax lines.')
        names = [t['name'].lower() for t in value]
        if len(set(names)) != len(names):
            raise serializers.ValidationError('Each tax needs a different name.')
        return value

    def validate(self, attrs):
        current = self.context['current']
        merged = {**current, **attrs}
        if merged['studio_open_hour'] >= merged['studio_close_hour']:
            raise serializers.ValidationError(
                {'studio_close_hour': 'Closing must be after opening (use 0 and 24 for round the clock).'})
        if merged['tax_enabled'] and not merged['taxes']:
            raise serializers.ValidationError({'taxes': 'Add at least one tax before switching tax on.'})
        for key in ('invoice_business_name', 'invoice_address', 'invoice_tin', 'invoice_contact',
                    'invoice_bank_details'):
            if key in attrs:
                attrs[key] = attrs[key].strip()
        if 'invoice_business_name' in attrs and not attrs['invoice_business_name']:
            raise serializers.ValidationError({'invoice_business_name': 'The business name is required.'})
        return attrs


def _present(include_meta: bool = True) -> dict:
    r = rules()
    data = {k: v for k, v in r.as_dict().items() if k in FIELDS}
    if include_meta:
        row = BusinessSettings.objects.filter(pk=BusinessSettings.SINGLETON_PK).select_related(
            'updated_by').first()
        data['saved'] = row is not None
        data['updated_at'] = row.updated_at if row else None
        data['updated_by'] = (row.updated_by.full_name or row.updated_by.email) if row and row.updated_by else None
    return data


class BusinessRulesPublicView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'public'

    def get(self, request):
        r = rules().as_dict()
        return Response({k: r[k] for k in PUBLIC_FIELDS} | {'taxes': r['taxes'] if r['tax_enabled'] else []})


class BusinessSettingsAdminView(APIView):
    permission_classes = [IsAuthenticated, IsONAAdmin]

    def get(self, request):
        return Response(_present())

    def patch(self, request):
        current = _present(include_meta=False)
        serializer = BusinessSettingsSerializer(data=request.data, partial=True,
                                                context={'current': current})
        serializer.is_valid(raise_exception=True)
        changes = {k: {'from': current[k], 'to': v} for k, v in serializer.validated_data.items()
                   if current[k] != v}
        if changes:
            row = BusinessSettings.objects.filter(pk=BusinessSettings.SINGLETON_PK).first()
            if row is None:
                row = BusinessSettings(**{k: v for k, v in from_environment().as_dict().items()
                                          if k in FIELDS})
            for key, value in serializer.validated_data.items():
                setattr(row, key, [dict(t) for t in value] if key == 'taxes' else value)
            row.updated_by = request.user
            row.save()
            AuditLog.record(AuditLog.Action.BUSINESS_SETTINGS_UPDATED, actor=request.user,
                            target=row, request=request, changes=changes)
        return Response(_present())
