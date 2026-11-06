"""
GET /api/v1/content/faqs/        published answers, for the public pages
/api/v1/admin/faqs/              staff CRUD, audited (config/admin_api.py)
"""

from rest_framework import mixins, viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle

from core.models import AuditLog
from core.permissions.roles import IsONAAdmin

from .models import Faq
from .serializers import FaqAdminSerializer, FaqPublicSerializer


class FaqPublicViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    permission_classes = [AllowAny]
    serializer_class = FaqPublicSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'public'
    pagination_class = None  # a handful of rows, shown all at once
    filter_backends = []

    def get_queryset(self):
        return Faq.objects.filter(is_published=True)


class FaqAdminViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsONAAdmin]
    serializer_class = FaqAdminSerializer
    search_fields = ('question', 'answer')
    filterset_fields = ('is_published',)

    def get_queryset(self):
        return Faq.objects.all()

    def perform_create(self, serializer):
        faq = serializer.save()
        AuditLog.record('content.faq_created', actor=self.request.user, target=faq,
                        request=self.request, question=faq.question)

    def perform_update(self, serializer):
        before = {k: getattr(serializer.instance, k) for k in ('question', 'answer', 'is_published')}
        faq = serializer.save()
        changed = {k: {'from': v, 'to': getattr(faq, k)} for k, v in before.items()
                   if getattr(faq, k) != v}
        AuditLog.record('content.faq_updated', actor=self.request.user, target=faq,
                        request=self.request, changes=changed)

    def perform_destroy(self, instance):
        AuditLog.record('content.faq_deleted', actor=self.request.user, target=instance,
                        request=self.request, question=instance.question, answer=instance.answer)
        instance.delete()
