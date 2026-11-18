"""
Site content staff edit from the console (Task 16): the FAQ, for now.

Answers are policy. They are written by ONA staff, published deliberately,
and every change is audited — a wrong answer on a public page is a promise
someone will hold the studio to.
"""

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
