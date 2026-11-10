from django.contrib import admin

from .models import NotificationLog


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    """Read-only: the answer to 'did the customer get told?'."""

    list_display = ('created_at', 'kind', 'recipient', 'status', 'attempts', 'sent_at', 'booking')
    list_filter = ('kind', 'status', 'channel')
    search_fields = ('recipient', 'booking__reference', 'dedupe_key')
    list_select_related = ('booking',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
