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

from .models import Job, Professional
from .serializers import JobSerializer, ProfessionalAdminSerializer


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


class JobAdminViewSet(viewsets.ModelViewSet):
    """
    /api/v1/admin/jobs/ — brokered work that is not a studio session (Task 15).

    Staff only. No delete: a job that falls through is cancelled, so the
    record of what was promised to whom survives. Every change is audited,
    with the fields that changed and the status before and after.
    """

    permission_classes = [IsAuthenticated, IsONAAdmin]
    serializer_class = JobSerializer
    http_method_names = ['get', 'post', 'patch', 'head', 'options']
    search_fields = ('reference', 'title', 'customer__email', 'customer__first_name',
                     'customer__last_name', 'professional__name', 'professional__stage_name')
    ordering_fields = ('created_at', 'due_on')
    filterset_fields = ('status', 'customer', 'professional')

    def get_queryset(self):
        return Job.objects.select_related('customer', 'professional', 'created_by')

    def perform_create(self, serializer):
        job = serializer.save(created_by=self.request.user)
        AuditLog.record('job.created', actor=self.request.user, target=job, request=self.request,
                        reference=job.reference, customer=job.customer.email,
                        professional=str(job.professional_id) if job.professional_id else None,
                        fee_pesewas=job.fee_pesewas)

    def perform_update(self, serializer):
        before = serializer.instance.status
        changed = sorted(serializer.validated_data)
        job = serializer.save()
        AuditLog.record('job.updated', actor=self.request.user, target=job, request=self.request,
                        reference=job.reference, fields=changed, previous_status=before,
                        status=job.status, fee_pesewas=job.fee_pesewas)
