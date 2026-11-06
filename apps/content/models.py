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
