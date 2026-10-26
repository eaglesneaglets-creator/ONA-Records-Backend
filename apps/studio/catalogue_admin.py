"""
/api/v1/admin/rooms|engineers|services|packages/ — staff edit the catalogue
(Task 16: "pricing and studio packages editable by staff").

PRICES AND BOOKINGS

Changing a rate never re-prices a booking: a booking's total is frozen when
it is made (StudioBooking.total_pesewas), and an issued invoice is frozen
with it. The new rate applies to bookings made from now on. Every change is
audited with the before and after of each field, so "when did the Live Room
go up?" has an answer.

Removing is a soft delete — a room that was booked must stay resolvable
from those bookings. To hide something without removing it, switch
`is_active` off.
"""

from rest_framework import serializers, viewsets
from rest_framework.permissions import IsAuthenticated

from core.models import AuditLog
from core.permissions.roles import IsONAAdmin

from .models import Engineer, Package, Room, Service

MAX_PRICE = 100_000_000  # GHS 1,000,000 — a typo guard, not a business rule


class _LiveNameUnique:
    """Names are unique among live rows (a DB constraint). Say so in words
    instead of letting the insert fail."""

    def validate_name(self, value):
        value = value.strip()
        qs = self.Meta.model.objects.filter(name__iexact=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('Another item already has this name.')
        return value


class RoomAdminSerializer(_LiveNameUnique, serializers.ModelSerializer):
    hourly_rate_display = serializers.CharField(read_only=True)

    class Meta:
        model = Room
        fields = ('id', 'name', 'description', 'hourly_rate_pesewas', 'hourly_rate_display',
                  'capacity', 'is_active', 'sort_order', 'updated_at')
        read_only_fields = ('id', 'updated_at')
        extra_kwargs = {'hourly_rate_pesewas': {'min_value': 1, 'max_value': MAX_PRICE}}


class EngineerAdminSerializer(_LiveNameUnique, serializers.ModelSerializer):
    class Meta:
        model = Engineer
        fields = ('id', 'name', 'bio', 'specialties', 'is_active', 'sort_order', 'updated_at')
        read_only_fields = ('id', 'updated_at')

    def validate_specialties(self, value):
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            raise serializers.ValidationError('Specialties must be a list of words.')
        return [s.strip() for s in value if s.strip()][:20]


class ServiceAdminSerializer(_LiveNameUnique, serializers.ModelSerializer):
    base_price_display = serializers.CharField(read_only=True)

    class Meta:
        model = Service
        fields = ('id', 'name', 'description', 'base_price_pesewas', 'base_price_display',
                  'duration_minutes', 'is_active', 'sort_order', 'updated_at')
        read_only_fields = ('id', 'updated_at')
        extra_kwargs = {'base_price_pesewas': {'min_value': 1, 'max_value': MAX_PRICE},
                        'duration_minutes': {'min_value': 15, 'max_value': 720}}


class PackageAdminSerializer(_LiveNameUnique, serializers.ModelSerializer):
    price_display = serializers.CharField(read_only=True)
    room = serializers.PrimaryKeyRelatedField(queryset=Room.objects.all(), required=False,
                                              allow_null=True)

    class Meta:
        model = Package
        fields = ('id', 'name', 'description', 'hours', 'price_pesewas', 'price_display',
                  'room', 'includes', 'is_active', 'sort_order', 'updated_at')
        read_only_fields = ('id', 'updated_at')
        extra_kwargs = {'price_pesewas': {'min_value': 1, 'max_value': MAX_PRICE},
                        'hours': {'min_value': 1, 'max_value': 12}}

    def validate_includes(self, value):
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            raise serializers.ValidationError('Includes must be a list of short lines.')
        return [s.strip() for s in value if s.strip()][:12]


def _audit_value(v):
    return str(v.pk) if hasattr(v, 'pk') else v


class CatalogueAdminViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsONAAdmin]
    search_fields = ('name',)
    filterset_fields = ('is_active',)
    ordering_fields = ('sort_order', 'name')
    kind = ''

    def get_queryset(self):
        return self.serializer_class.Meta.model.objects.all()

    def perform_create(self, serializer):
        item = serializer.save()
        AuditLog.record('catalogue.created', actor=self.request.user, target=item,
                        request=self.request, kind=self.kind, name=item.name,
                        fields={k: _audit_value(v) for k, v in serializer.validated_data.items()})

    def perform_update(self, serializer):
        before = {k: _audit_value(getattr(serializer.instance, k)) for k in serializer.validated_data}
        item = serializer.save()
        changes = {k: {'from': before[k], 'to': _audit_value(getattr(item, k))}
                   for k in before if before[k] != _audit_value(getattr(item, k))}
        if changes:
            AuditLog.record('catalogue.updated', actor=self.request.user, target=item,
                            request=self.request, kind=self.kind, name=item.name, changes=changes)

    def perform_destroy(self, instance):
        instance.soft_delete(by=self.request.user)
        AuditLog.record('catalogue.removed', actor=self.request.user, target=instance,
                        request=self.request, kind=self.kind, name=instance.name)


class RoomAdminViewSet(CatalogueAdminViewSet):
    serializer_class, kind = RoomAdminSerializer, 'room'


class EngineerAdminViewSet(CatalogueAdminViewSet):
    serializer_class, kind = EngineerAdminSerializer, 'engineer'


class ServiceAdminViewSet(CatalogueAdminViewSet):
    serializer_class, kind = ServiceAdminSerializer, 'service'


class PackageAdminViewSet(CatalogueAdminViewSet):
    serializer_class, kind = PackageAdminSerializer, 'package'

    def get_queryset(self):
        return Package.objects.select_related('room')
