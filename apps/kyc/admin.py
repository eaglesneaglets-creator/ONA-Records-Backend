from django.contrib import admin

from .models import IdentityCheck


@admin.register(IdentityCheck)
class IdentityCheckAdmin(admin.ModelAdmin):
    """Read-only: decisions are made in the staff console, through the KYC service."""

    list_display = ['user', 'status', 'submitted_at', 'decided_at', 'updated_at']
    list_filter = ['status']
    search_fields = ['user__email', 'verification_id']
    readonly_fields = [f.name for f in IdentityCheck._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
