from django.contrib import admin

from .models import Professional


@admin.register(Professional)
class ProfessionalAdmin(admin.ModelAdmin):
    """Back-office fallback; staff normally use the ONA admin dashboard."""

    list_display = ('name', 'stage_name', 'rate_display', 'is_active', 'user', 'updated_at')
    list_filter = ('is_active', 'rate_type')
    search_fields = ('name', 'stage_name', 'email', 'phone')
    readonly_fields = ('id', 'user', 'created_at', 'updated_at', 'deleted_at', 'deleted_by')

    def get_queryset(self, request):
        return Professional.all_objects.get_queryset()
