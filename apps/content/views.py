"""
GET /api/v1/content/faqs/         published answers, for the public pages
GET /api/v1/content/contact/      how to reach ONA, and its social profiles
GET /api/v1/content/site/         homepage hero, gallery, reviews, Google rating
GET /api/v1/content/media/<id>/   a website image (cached for good: never changes)
/api/v1/admin/contact/            staff edit the contact details, audited
/api/v1/admin/faqs/               staff CRUD, audited (config/admin_api.py)
/api/v1/admin/site/               the hero and the Google summary, audited
/api/v1/admin/media/              the media library: upload, describe, delete
/api/v1/admin/gallery/            gallery photos, audited
/api/v1/admin/testimonials/       the quoted reviews, audited
"""

from django.http import FileResponse, Http404
from rest_framework import mixins, status, viewsets
from rest_framework.exceptions import APIException
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle

from core import storage
from core.models import AuditLog
from core.permissions.roles import IsONAAdmin

from .media import process_upload
from .models import ContactDetails, Faq, GalleryImage, MediaAsset, SiteContent, Testimonial
from .serializers import (
    CONTACT_FIELDS,
    GOOGLE_FIELDS,
    HERO_FIELDS,
    ContactAdminSerializer,
    ContactPublicSerializer,
    FaqAdminSerializer,
    FaqPublicSerializer,
    GalleryImageAdminSerializer,
    MediaAssetAdminSerializer,
    SiteContentAdminSerializer,
    TestimonialAdminSerializer,
    site_public_payload,
)


class ImageInUse(APIException):
    """409: the site still shows this image. Raised (not returned) so the API's error envelope carries the message."""

    status_code = status.HTTP_409_CONFLICT
    default_code = 'in_use'
    default_detail = 'This image is still shown on the site.'


def _changes(before: dict, obj, fields) -> dict:
    """{field: {from, to}} for the fields an edit changed, as audit metadata."""
    out = {}
    for f in fields:
        new = getattr(obj, f)
        if before.get(f) != new:
            out[f] = {'from': _plain(before.get(f)), 'to': _plain(new)}
    return out


def _plain(value):
    """A value the audit log can store as JSON (a related object becomes its id)."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(getattr(value, 'pk', value))


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


# ── The public website's own content ────────────────────────────────────────


class SitePublicView(APIView):
    """GET /api/v1/content/site/ — hero, gallery, reviews and the Google rating, for the public pages."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'public'

    def get(self, request):
        return Response(site_public_payload())


class MediaPublicView(APIView):
    """
    GET /api/v1/content/media/<id>/ — the bytes of a website image.

    The bucket is private and its signed links expire, which would break any
    page or cache holding one. So the API streams the file itself, and since
    an asset never changes after upload, it tells every cache to keep it for
    a year.
    """

    permission_classes = [AllowAny]
    authentication_classes = []  # public bytes: no session lookup, no CSRF
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'media'

    def get(self, request, pk):
        asset = MediaAsset.objects.filter(pk=pk).only('key').first()
        if asset is None:
            raise Http404
        stream = storage.open_stream(asset.key)
        if stream is None:
            raise Http404
        body, content_type, length = stream
        response = FileResponse(body, content_type=content_type)
        if length:
            response['Content-Length'] = str(length)
        response['Cache-Control'] = 'public, max-age=31536000, immutable'
        response['Content-Disposition'] = 'inline'
        return response


class SiteContentAdminView(APIView):
    """GET/PATCH /api/v1/admin/site/ — the homepage hero and the Google rating summary. Audited."""

    permission_classes = [IsAuthenticated, IsONAAdmin]

    def get(self, request):
        return Response(SiteContentAdminSerializer(SiteContent.load()).data)

    def patch(self, request):
        site = SiteContent.load()
        fields = (*HERO_FIELDS, *GOOGLE_FIELDS)
        before = {f: getattr(site, f) for f in fields}
        serializer = SiteContentAdminSerializer(site, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        site = serializer.save()
        changed = _changes(before, site, fields)
        if changed:
            AuditLog.record('content.site_updated', actor=request.user, target=site, request=request, changes=changed)
        return Response(SiteContentAdminSerializer(site).data)


class MediaAdminViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin,
                        mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """
    The media library. POST a multipart `file` (and optional `alt_text`) to
    upload; PATCH `alt_text` to describe; DELETE refuses an image the site
    still shows.
    """

    permission_classes = [IsAuthenticated, IsONAAdmin]
    serializer_class = MediaAssetAdminSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    search_fields = ('alt_text',)
    filter_backends = []

    def get_queryset(self):
        return MediaAsset.objects.all()

    def create(self, request):
        stored = process_upload(request.FILES.get('file'))
        alt = (request.data.get('alt_text') or '').strip()[:200]
        asset = MediaAsset.objects.create(alt_text=alt, uploaded_by=request.user, **stored)
        AuditLog.record('content.media_uploaded', actor=request.user, target=asset, request=request,
                        bytes=asset.bytes, width=asset.width, height=asset.height)
        return Response(self.get_serializer(asset).data, status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        before = {'alt_text': serializer.instance.alt_text}
        asset = serializer.save()
        changed = _changes(before, asset, ('alt_text',))
        if changed:
            AuditLog.record('content.media_updated', actor=self.request.user, target=asset,
                            request=self.request, changes=changed)

    def destroy(self, request, *args, **kwargs):
        asset = self.get_object()
        uses = asset.used_by()
        if uses:
            raise ImageInUse(f"This image is still shown on {' and '.join(uses)}. Replace it there first.")
        key = asset.key
        AuditLog.record('content.media_deleted', actor=request.user, target=asset, request=request,
                        alt_text=asset.alt_text)
        asset.delete()
        storage.delete(key)  # a failed delete leaves an orphan, not a broken page
        return Response(status=status.HTTP_204_NO_CONTENT)


class _AuditedCrud(viewsets.ModelViewSet):
    """CRUD whose every change is in the audit log, with what it said before."""

    permission_classes = [IsAuthenticated, IsONAAdmin]
    audit_name = ''
    audit_fields: tuple = ()

    def perform_create(self, serializer):
        obj = serializer.save()
        AuditLog.record(f'content.{self.audit_name}_created', actor=self.request.user, target=obj,
                        request=self.request, **{f: _plain(getattr(obj, f)) for f in self.audit_fields})

    def perform_update(self, serializer):
        before = {f: getattr(serializer.instance, f) for f in self.audit_fields}
        obj = serializer.save()
        changed = _changes(before, obj, self.audit_fields)
        if changed:
            AuditLog.record(f'content.{self.audit_name}_updated', actor=self.request.user, target=obj,
                            request=self.request, changes=changed)

    def perform_destroy(self, instance):
        AuditLog.record(f'content.{self.audit_name}_deleted', actor=self.request.user, target=instance,
                        request=self.request, **{f: _plain(getattr(instance, f)) for f in self.audit_fields})
        instance.delete()


class GalleryAdminViewSet(_AuditedCrud):
    serializer_class = GalleryImageAdminSerializer
    audit_name = 'gallery'
    audit_fields = ('media', 'caption', 'is_published', 'sort_order')
    pagination_class = None  # a page of photos, shown and reordered all at once
    filter_backends = []

    def get_queryset(self):
        return GalleryImage.objects.select_related('media')


class TestimonialAdminViewSet(_AuditedCrud):
    serializer_class = TestimonialAdminSerializer
    audit_name = 'testimonial'
    audit_fields = ('quote', 'author', 'rating', 'source', 'is_published', 'sort_order')
    pagination_class = None
    filter_backends = []

    def get_queryset(self):
        return Testimonial.objects.all()
