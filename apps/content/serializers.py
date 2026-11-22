from urllib.parse import urlparse

from django.urls import reverse
from rest_framework import serializers

from core.validators import clean_phone

from .models import ContactDetails, Faq, GalleryImage, MediaAsset, SiteContent, Testimonial


class FaqPublicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Faq
        fields = ('id', 'question', 'answer')
        read_only_fields = fields


class FaqAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = Faq
        fields = ('id', 'question', 'answer', 'is_published', 'sort_order', 'created_at',
                  'updated_at')
        read_only_fields = ('id', 'created_at', 'updated_at')

    def validate_question(self, value):
        return value.strip()

    def validate_answer(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('An answer is required.')
        return value


SOCIAL_FIELDS = ('instagram_url', 'facebook_url', 'linkedin_url', 'tiktok_url', 'youtube_url')
CONTACT_FIELDS = ('phone', 'whatsapp', 'email', 'address', 'directions_url', *SOCIAL_FIELDS)

#: The domains each social link must point at (and their subdomains).
SOCIAL_HOSTS = {
    'instagram_url': ('instagram.com',),
    'facebook_url': ('facebook.com', 'fb.com'),
    'linkedin_url': ('linkedin.com',),
    'tiktok_url': ('tiktok.com',),
    'youtube_url': ('youtube.com', 'youtu.be'),
}
SOCIAL_NAMES = {
    'instagram_url': 'Instagram', 'facebook_url': 'Facebook', 'linkedin_url': 'LinkedIn',
    'tiktok_url': 'TikTok', 'youtube_url': 'YouTube',
}


def clean_social(field: str, value: str) -> str:
    """An https link on the right network, without tracking parameters, or ''."""
    value = (value or '').strip()
    if not value:
        return ''
    url = urlparse(value)
    host = (url.hostname or '').lower()
    if url.scheme != 'https' or not any(host == h or host.endswith('.' + h) for h in SOCIAL_HOSTS[field]):
        raise serializers.ValidationError(f'Paste the full https link to ONA on {SOCIAL_NAMES[field]}.')
    # Drop the query (?originalSubdomain=gh, ?igsh=…): it tracks the person
    # who copied the link, and the profile is the same without it.
    return url._replace(query='', fragment='').geturl()



class ContactPublicSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactDetails
        fields = CONTACT_FIELDS
        read_only_fields = fields


class ContactAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactDetails
        fields = (*CONTACT_FIELDS, 'updated_at')
        read_only_fields = ('updated_at',)

    def validate_phone(self, value):
        return clean_phone(value)

    def validate_whatsapp(self, value):
        return clean_phone(value)

    def validate_address(self, value):
        return value.strip()

    def validate(self, attrs):
        errors = {}
        for field in SOCIAL_FIELDS:
            if field in attrs:
                try:
                    attrs[field] = clean_social(field, attrs[field])
                except serializers.ValidationError as e:
                    errors[field] = e.detail
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


# ── The website's images, hero, gallery and reviews ─────────────────────────


def media_payload(asset: MediaAsset | None) -> dict | None:
    """How a page draws an image: a stable URL path plus its size and description."""
    if asset is None:
        return None
    return {
        'id': str(asset.pk),
        'src': reverse('content:media', args=[asset.pk]),
        'width': asset.width,
        'height': asset.height,
        'alt': asset.alt_text,
    }


class MediaAssetAdminSerializer(serializers.ModelSerializer):
    src = serializers.SerializerMethodField()
    used_by = serializers.SerializerMethodField()
    is_small = serializers.BooleanField(read_only=True)

    class Meta:
        model = MediaAsset
        fields = ('id', 'src', 'width', 'height', 'bytes', 'alt_text', 'used_by', 'created_at',
                  'is_small', 'enhance_status', 'enhance_error', 'enhanced_from')
        read_only_fields = ('id', 'src', 'width', 'height', 'bytes', 'used_by', 'created_at',
                            'is_small', 'enhance_status', 'enhance_error', 'enhanced_from')

    def get_src(self, obj):
        return reverse('content:media', args=[obj.pk])

    def get_used_by(self, obj):
        return obj.used_by()

    def validate_alt_text(self, value):
        return value.strip()


HERO_FIELDS = ('hero_eyebrow', 'hero_title', 'hero_body', 'hero_image', 'hero_focus')
GOOGLE_FIELDS = ('google_rating', 'google_review_count', 'google_reviews_url')


class SiteContentAdminSerializer(serializers.ModelSerializer):
    hero_image = serializers.PrimaryKeyRelatedField(queryset=MediaAsset.objects.all(), allow_null=True, required=False)
    hero_image_detail = serializers.SerializerMethodField()

    class Meta:
        model = SiteContent
        fields = (*HERO_FIELDS, 'hero_image_detail', *GOOGLE_FIELDS, 'updated_at')
        read_only_fields = ('hero_image_detail', 'updated_at')

    def get_hero_image_detail(self, obj):
        return media_payload(obj.hero_image)

    def validate(self, attrs):
        for f in ('hero_eyebrow', 'hero_title', 'hero_body'):
            if f in attrs:
                attrs[f] = attrs[f].strip()
        if 'hero_title' in attrs:
            # At most three lines: the hero is designed for two.
            lines = [ln.strip() for ln in attrs['hero_title'].splitlines() if ln.strip()]
            if len(lines) > 3:
                raise serializers.ValidationError({'hero_title': 'Keep the headline to three lines or fewer.'})
            attrs['hero_title'] = '\n'.join(lines)
        return attrs


class GalleryImageAdminSerializer(serializers.ModelSerializer):
    media = serializers.PrimaryKeyRelatedField(queryset=MediaAsset.objects.all())
    media_detail = serializers.SerializerMethodField()

    class Meta:
        model = GalleryImage
        fields = ('id', 'media', 'media_detail', 'caption', 'is_published', 'sort_order', 'created_at')
        read_only_fields = ('id', 'media_detail', 'created_at')

    def get_media_detail(self, obj):
        return media_payload(obj.media)

    def validate_caption(self, value):
        return value.strip()


class TestimonialAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = Testimonial
        fields = ('id', 'quote', 'author', 'rating', 'source', 'is_published', 'sort_order', 'created_at')
        read_only_fields = ('id', 'created_at')

    def validate_quote(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('The review text is required.')
        return value

    def validate_author(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Who wrote it is required.')
        return value


def site_public_payload() -> dict:
    """Everything the public pages draw from the console, in one response."""
    site = SiteContent.objects.select_related('hero_image').get_or_create(pk=SiteContent.SINGLETON_PK)[0]
    gallery = GalleryImage.objects.filter(is_published=True).select_related('media')
    reviews = Testimonial.objects.filter(is_published=True)
    google = None
    if site.google_rating and site.google_review_count:
        google = {
            'rating': str(site.google_rating),
            'count': site.google_review_count,
            'url': site.google_reviews_url,
        }
    return {
        'hero': {
            'eyebrow': site.hero_eyebrow,
            'title': site.hero_title,
            'body': site.hero_body,
            'image': media_payload(site.hero_image),
            'focus': site.hero_focus,
        },
        'gallery': [{'id': str(g.pk), 'caption': g.caption, 'image': media_payload(g.media)} for g in gallery],
        'reviews': [
            {'id': str(t.pk), 'quote': t.quote, 'author': t.author, 'rating': t.rating, 'source': t.source}
            for t in reviews
        ],
        'google': google,
    }
