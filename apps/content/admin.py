from django.contrib import admin

from .models import Faq


@admin.register(Faq)
class FaqAdmin(admin.ModelAdmin):
    list_display = ('question', 'is_published', 'sort_order', 'updated_at')
    list_filter = ('is_published',)
    search_fields = ('question', 'answer')
