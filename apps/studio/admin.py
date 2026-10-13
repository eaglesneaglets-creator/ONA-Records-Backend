"""
Django admin for the catalogue.

This is the staff tool until the ONA admin dashboard (Phase 3) exists, and
stays as the back-office fallback after. Money columns show the formatted
GHS value next to the raw pesewas so a typo of one zero is visible at a
glance rather than discovered by a customer.
"""

from django import forms
from django.contrib import admin, messages
from django.utils import timezone

from .models import BookingStatus, CalendarBlock, Engineer, Room, Service, StudioBooking


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
    # room/engineer are nullable, which admin's automatic select_related
    # skips — two extra queries per row without this.
    list_select_related = ('room', 'engineer')
    list_filter = ('kind', 'room', 'engineer')
    date_hierarchy = 'start_at'
    ordering = ('-start_at',)


class StudioBookingAdminForm(forms.ModelForm):
    class Meta:
        model = StudioBooking
        fields = '__all__'

    def _get_validation_exclusions(self):
        # deleted_at is read-only here, so the form excludes it from model
        # validation — and the exclusion constraints' condition references it,
        # which raised FieldError (a 500) on every save. Keeping it in lets
        # Django validate the overlap rule and show a form error instead.
        exclude = super()._get_validation_exclusions()
        exclude.discard('deleted_at')
        return exclude


@admin.register(StudioBooking)
class StudioBookingAdmin(admin.ModelAdmin):
    form = StudioBookingAdminForm
    actions = ('cancel_bookings', 'soft_delete_bookings')
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

    def has_add_permission(self, request):
        # Bookings are created through the API (and, in Phase 3, the ONA
        # admin dashboard), which prices them. total_pesewas is frozen and
        # read-only here, so an admin-created row could not be priced.
        return False

    def has_delete_permission(self, request, obj=None):
        # A booking is a money record. Cancel it or soft-delete it; never
        # remove the row.
        return False

    @admin.action(description='Cancel selected bookings (releases the slot)')
    def cancel_bookings(self, request, queryset):
        n = queryset.exclude(status=BookingStatus.CANCELLED).update(
            status=BookingStatus.CANCELLED, updated_at=timezone.now())
        self.message_user(request, f'{n} booking(s) cancelled.', messages.SUCCESS)

    @admin.action(description='Soft-delete selected bookings')
    def soft_delete_bookings(self, request, queryset):
        n = 0
        for booking in queryset.filter(deleted_at__isnull=True):
            booking.soft_delete(by=request.user)
            n += 1
        self.message_user(request, f'{n} booking(s) soft-deleted.', messages.SUCCESS)
