"""
GET /api/v1/content/faqs/        published answers, for the public pages
GET /api/v1/content/contact/     how to reach ONA
/api/v1/admin/contact/           staff edit the contact details, audited
/api/v1/admin/faqs/              staff CRUD, audited (config/admin_api.py)
"""

from rest_framework import mixins, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle

from core.models import AuditLog
from core.permissions.roles import IsONAAdmin

from .models import ContactDetails, Faq
from .serializers import (
    CONTACT_FIELDS,
    ContactAdminSerializer,
    ContactPublicSerializer,
    FaqAdminSerializer,
    FaqPublicSerializer,
)


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


class ContactPublicView(APIView):
    """GET /api/v1/content/contact/ — how to reach ONA (blank fields are omitted by the page)."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'public'

    def get(self, request):
        return Response(ContactPublicSerializer(ContactDetails.load()).data)


class ContactAdminView(APIView):
    """GET/PATCH /api/v1/admin/contact/ — staff keep the contact details current. Audited."""

    permission_classes = [IsAuthenticated, IsONAAdmin]

    def get(self, request):
        return Response(ContactAdminSerializer(ContactDetails.load()).data)

    def patch(self, request):
        contact = ContactDetails.load()
        before = {f: getattr(contact, f) for f in CONTACT_FIELDS}
        serializer = ContactAdminSerializer(contact, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        contact = serializer.save()
        changed = {f: {'from': v, 'to': getattr(contact, f)} for f, v in before.items()
                   if getattr(contact, f) != v}
        if changed:
            AuditLog.record(AuditLog.Action.CONTACT_UPDATED, actor=request.user, target=contact,
                            request=request, changes=changed)
        return Response(ContactAdminSerializer(contact).data)
