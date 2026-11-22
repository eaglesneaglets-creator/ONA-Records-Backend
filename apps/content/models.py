"""
Site content staff edit from the console (Task 16): the FAQ, contact details
and social links, and the public website's own content: the homepage hero,
the photo gallery, the reviews section and the images they use.

Answers are policy. They are written by ONA staff, published deliberately,
and every change is audited — a wrong answer on a public page is a promise
someone will hold the studio to.
"""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from core.mixins.timestamp import TimestampMixin, UUIDPrimaryKeyMixin


class Faq(UUIDPrimaryKeyMixin, TimestampMixin):
    question = models.CharField(max_length=200)
    answer = models.TextField(max_length=2000)
    is_published = models.BooleanField(default=True, db_index=True)
    sort_order = models.PositiveSmallIntegerField(default=100, help_text='Lower first.')

    class Meta:
        ordering = ('sort_order', 'created_at')
        verbose_name = 'FAQ'

    def __str__(self) -> str:
        return self.question


class ContactDetails(TimestampMixin):
    """
    How customers reach ONA: one row, edited by staff, shown on /contact and
    referred to by the privacy policy. Blank fields are simply not shown —
    the site never invents a number or an address.
    """

    SINGLETON_PK = 1

    phone = models.CharField(max_length=30, blank=True)
    whatsapp = models.CharField(max_length=30, blank=True, help_text='Number for WhatsApp, if different.')
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=300, blank=True, help_text='Where the studio is.')
    directions_url = models.URLField(blank=True, help_text='A Google Maps link to the studio.')

    # ONA's own profiles. Shown in the footer and given to search engines as
    # the same business (schema.org sameAs). Checked against each network's
    # domain (serializers.SOCIAL_HOSTS), so a typo cannot link elsewhere.
    instagram_url = models.URLField(blank=True)
    facebook_url = models.URLField(blank=True)
    linkedin_url = models.URLField(blank=True)
    tiktok_url = models.URLField(blank=True)
    youtube_url = models.URLField(blank=True)

    class Meta:
        verbose_name = 'contact details'
        verbose_name_plural = 'contact details'

    def __str__(self) -> str:
        return 'ONA contact details'

    @classmethod
    def load(cls) -> 'ContactDetails':
        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return obj

    @property
    def is_empty(self) -> bool:
        return not (self.phone or self.whatsapp or self.email)


class MediaAsset(UUIDPrimaryKeyMixin, TimestampMixin):
    """
    An image in the website's media library: the hero, gallery photos.

    Every upload is re-encoded to WebP (apps/content/media.py), which drops
    the camera's EXIF data, including the GPS position a phone photo carries.
    The file is never changed after upload, so the public URL can be cached
    for good: a new picture is a new asset.
    """

    key = models.CharField(max_length=300, unique=True, help_text='Where the file is in the bucket.')
    content_type = models.CharField(max_length=50, default='image/webp')
    bytes = models.PositiveIntegerField()
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    alt_text = models.CharField(
        max_length=200, blank=True,
        help_text='What the picture shows, for people using screen readers and for search engines.',
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
    )

    # Enhancing (AI upscaling) never changes this file: it makes a new asset
    # whose enhanced_from points here, and moves the site's uses onto it.
    class Enhance(models.TextChoices):
        NONE = '', 'Not requested'
        PENDING = 'pending', 'Enhancing'
        DONE = 'done', 'Enhanced'
        FAILED = 'failed', 'Failed'

    enhanced_from = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL, related_name='enhancements',
        help_text='The photo this one was upscaled from.',
    )
    enhance_status = models.CharField(max_length=10, choices=Enhance.choices, default='', blank=True)
    enhance_error = models.CharField(max_length=200, blank=True)
    enhance_requested_at = models.DateTimeField(null=True, blank=True)

    #: Below this on its long side a photo looks soft on a laptop or a
    #: high-density phone screen (a full-width hero wants ~2400 px).
    SMALL_EDGE = 1600

    class Meta:
        ordering = ('-created_at',)

    def __str__(self) -> str:
        return self.alt_text or str(self.pk)

    @property
    def is_small(self) -> bool:
        return max(self.width, self.height) < self.SMALL_EDGE

    def used_by(self) -> list[str]:
        """Where the site shows this image, in words; empty when it can be deleted."""
        uses = []
        if SiteContent.objects.filter(hero_image=self).exists():
            uses.append('the homepage hero')
        n = self.gallery_items.count()
        if n:
            uses.append('the gallery' if n == 1 else f'the gallery ({n} times)')
        return uses


class SiteContent(TimestampMixin):
    """
    The homepage's words and picture, and the Google rating summary: one
    row, edited by ONA admins. A blank field means "use the built-in
    default", so the site never shows an empty hero.
    """

    SINGLETON_PK = 1

    hero_eyebrow = models.CharField(max_length=60, blank=True, help_text='Small label above the headline.')
    hero_title = models.CharField(
        max_length=120, blank=True, help_text='The headline. A line break starts a new line.',
    )
    hero_body = models.CharField(max_length=300, blank=True, help_text='The sentence under the headline.')
    hero_image = models.ForeignKey(
        MediaAsset, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
    )
    hero_focus = models.PositiveSmallIntegerField(
        default=70, validators=[MaxValueValidator(100)],
        help_text='Which part of the photo stays in view on a phone: 0 = left edge, 100 = right edge.',
    )

    # Copied by an admin from the Google Business Profile. The site shows it
    # as a link to Google, never as review markup (Google ignores ratings a
    # business publishes about itself).
    google_rating = models.DecimalField(
        max_digits=2, decimal_places=1, null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    google_review_count = models.PositiveIntegerField(null=True, blank=True)
    google_reviews_url = models.URLField(blank=True, help_text='The Google Maps link to ONA and its reviews.')

    class Meta:
        verbose_name = 'site content'
        verbose_name_plural = 'site content'

    def __str__(self) -> str:
        return 'ONA site content'

    @classmethod
    def load(cls) -> 'SiteContent':
        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return obj


class GalleryImage(UUIDPrimaryKeyMixin, TimestampMixin):
    """A photo in the public gallery: an image from the library plus an optional caption."""

    media = models.ForeignKey(MediaAsset, on_delete=models.PROTECT, related_name='gallery_items')
    caption = models.CharField(max_length=140, blank=True)
    is_published = models.BooleanField(default=True, db_index=True)
    sort_order = models.PositiveSmallIntegerField(default=100, help_text='Lower first.')

    class Meta:
        ordering = ('sort_order', 'created_at')

    def __str__(self) -> str:
        return self.caption or str(self.media)


class Testimonial(UUIDPrimaryKeyMixin, TimestampMixin):
    """
    A review quoted on the public site, chosen by ONA from reviews people
    left elsewhere (Google, Facebook). Quoted as written; the author is shown
    the way the admin enters it (first name and initial, by convention).
    """

    class Source(models.TextChoices):
        GOOGLE = 'google', 'Google'
        FACEBOOK = 'facebook', 'Facebook'
        DIRECT = 'direct', 'Sent to ONA'

    quote = models.TextField(max_length=600)
    author = models.CharField(max_length=80)
    rating = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.GOOGLE)
    is_published = models.BooleanField(default=True, db_index=True)
    sort_order = models.PositiveSmallIntegerField(default=100, help_text='Lower first.')

    class Meta:
        ordering = ('sort_order', 'created_at')

    def __str__(self) -> str:
        return f'{self.author}: {self.quote[:40]}'
