"""
Django admin for the catalogue.

This is the staff tool until the ONA admin dashboard (Phase 3) exists, and
stays as the back-office fallback after. Money columns show the formatted
GHS value next to the raw pesewas so a typo of one zero is visible at a
glance rather than discovered by a customer.
"""

from django.contrib import admin

from .models import CalendarBlock, Engineer, Room, Service, StudioBooking


class CatalogueAdmin(admin.ModelAdmin):
    list_filter = ('is_active',)
    search_fields = ('name', 'description')
    ordering = ('sort_order', 'name')
    readonly_fields = ('id', 'created_at', 'updated_at', 'deleted_at', 'deleted_by')
    list_per_page = 50

    def get_queryset(self, request):
        # Deleted rows are visible here — admin is an audit surface, and a
        # restore has to be possible from somewhere.
        return self.model.all_objects.get_queryset()


@admin.register(Room)
class RoomAdmin(CatalogueAdmin):
    list_display = ('name', 'hourly_rate_display', 'hourly_rate_pesewas', 'capacity',
                    'is_active', 'sort_order', 'updated_at')
    list_editable = ('is_active', 'sort_order')
    fields = ('name', 'description', 'hourly_rate_pesewas', 'capacity', 'is_active',
              'sort_order', 'id', 'created_at', 'updated_at', 'deleted_at', 'deleted_by')


@admin.register(Engineer)
class EngineerAdmin(CatalogueAdmin):
    list_display = ('name', 'specialties', 'is_active', 'sort_order', 'updated_at')
    list_editable = ('is_active', 'sort_order')
    fields = ('name', 'bio', 'specialties', 'is_active', 'sort_order',
              'id', 'created_at', 'updated_at', 'deleted_at', 'deleted_by')


@admin.register(Service)
class ServiceAdmin(CatalogueAdmin):
    list_display = ('name', 'base_price_display', 'base_price_pesewas', 'duration_minutes',
                    'is_active', 'sort_order', 'updated_at')
    list_editable = ('is_active', 'sort_order')
    fields = ('name', 'description', 'base_price_pesewas', 'duration_minutes', 'is_active',
              'sort_order', 'id', 'created_at', 'updated_at', 'deleted_at', 'deleted_by')


@admin.register(CalendarBlock)
class CalendarBlockAdmin(admin.ModelAdmin):
    list_display = ('kind', 'room', 'engineer', 'start_at', 'end_at', 'reason')
    list_filter = ('kind', 'room', 'engineer')
    date_hierarchy = 'start_at'
    ordering = ('-start_at',)


@admin.register(StudioBooking)
class StudioBookingAdmin(admin.ModelAdmin):
    """
    Read-mostly. Creating a booking here bypasses availability validation;
    the exclusion constraint still refuses an overlap, but as a raw database
    error rather than a sentence. Staff create bookings through the ONA
    admin dashboard (Phase 3), which validates first.
    """
    list_display = ('reference', 'customer', 'room', 'engineer', 'start_at', 'end_at',
                    'status', 'total_display')
    list_filter = ('status', 'room', 'engineer')
    search_fields = ('reference', 'customer__email', 'customer__first_name',
                     'customer__last_name')
    date_hierarchy = 'start_at'
    ordering = ('-start_at',)
    readonly_fields = ('id', 'reference', 'total_pesewas', 'created_at', 'updated_at',
                       'deleted_at', 'deleted_by')
    raw_id_fields = ('customer',)

    def get_queryset(self, request):
        return StudioBooking.all_objects.get_queryset().select_related(
            'customer', 'room', 'engineer')
