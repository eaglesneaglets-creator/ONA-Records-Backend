"""
/api/v1/admin/professionals/ — staff CRUD for professional records.

ONA staff only (IsONAAdmin). Delete is a soft delete: a professional who was
assigned to past sessions must stay resolvable from those bookings.
Every change is written to AuditLog.
"""

from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from core.models import AuditLog
from core.permissions.roles import IsONAAdmin

from .models import Professional
from .serializers import ProfessionalAdminSerializer


class ProfessionalAdminViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsONAAdmin]
    serializer_class = ProfessionalAdminSerializer
    search_fields = ('name', 'stage_name', 'email', 'phone')
    ordering_fields = ('name', 'created_at')
    filterset_fields = ('is_active', 'rate_type')

    def get_queryset(self):
        return Professional.objects.all()

    def perform_create(self, serializer):
        pro = serializer.save()
        AuditLog.record('professional.created', actor=self.request.user, target=pro,
                        request=self.request, name=pro.name)

    def perform_update(self, serializer):
        changed = sorted(serializer.validated_data)
        pro = serializer.save()
        AuditLog.record('professional.updated', actor=self.request.user, target=pro,
                        request=self.request, fields=changed)

    def perform_destroy(self, instance):
        instance.soft_delete(by=self.request.user)
        AuditLog.record('professional.removed', actor=self.request.user, target=instance,
                        request=self.request, name=instance.name)
