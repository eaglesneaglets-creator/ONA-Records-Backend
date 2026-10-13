"""
Read-only admin for money.

Staff can see every transaction and ledger entry; nobody can add, edit or
delete one here. Money moves only through apps/payments/ledger.py, which
holds the locks and enforces the balance rules. A form that let staff type
a ledger row would bypass every one of them.
"""

from django.contrib import admin

from .models import Commission, LedgerEntry, Transaction


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class LedgerEntryInline(admin.TabularInline):
    model = LedgerEntry
    extra = 0
    can_delete = False
    fields = ('entry_type', 'amount_pesewas', 'beneficiary', 'created_at', 'created_by', 'note')
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Transaction)
class TransactionAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'customer', 'booking', 'amount_display', 'status',
                    'provider', 'provider_ref', 'commission_percent')
    list_filter = ('status', 'provider')
    search_fields = ('provider_ref', 'customer__email', 'booking__reference')
    list_select_related = ('customer', 'booking')
    inlines = (LedgerEntryInline,)


@admin.register(LedgerEntry)
class LedgerEntryAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'entry_type', 'amount_pesewas', 'beneficiary',
                    'transaction', 'created_by')
    list_filter = ('entry_type', 'beneficiary')
    list_select_related = ('transaction', 'created_by')


@admin.register(Commission)
class CommissionAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'percent', 'amount_pesewas', 'ledger_entry')
    list_select_related = ('ledger_entry',)
