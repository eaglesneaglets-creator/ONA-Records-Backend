"""
ONA's business rules (deposit, holds, notice, hours, fees, commission, tax,
invoice details) as one read-only object.

    from core.business import rules
    rules().booking_deposit_percent

Staff edit them in the console (core.models.BusinessSettings). Until they
first save, or if the database cannot be read, every value comes from the
environment (config/settings/base.py), so behaviour is exactly what it was.
The saved row is cached for CACHE_SECONDS (Redis in production, so every
worker sees a change at once; saving clears it).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from django.conf import settings
from django.core.cache import cache

CACHE_KEY = 'ona:business-settings:v1'
CACHE_SECONDS = 30
_NO_ROW = '__none__'

FIELDS = (
    'booking_deposit_percent', 'unpaid_hold_minutes', 'free_cancellation_hours',
    'deposit_credit_days', 'max_open_unpaid', 'reminder_hours_before',
    'studio_open_hour', 'studio_close_hour',
    'pass_paystack_fee', 'paystack_fee_basis_points',
    'studio_commission_percent', 'marketplace_commission_percent',
    'tax_enabled', 'taxes',
    'invoice_business_name', 'invoice_address', 'invoice_tin', 'invoice_contact',
    'invoice_bank_details',
)


@dataclass(frozen=True)
class Rules:
    booking_deposit_percent: int
    unpaid_hold_minutes: int
    free_cancellation_hours: int
    deposit_credit_days: int
    max_open_unpaid: int
    reminder_hours_before: int
    studio_open_hour: int
    studio_close_hour: int
    pass_paystack_fee: bool
    paystack_fee_basis_points: int
    studio_commission_percent: int
    marketplace_commission_percent: int
    tax_enabled: bool
    taxes: tuple
    invoice_business_name: str
    invoice_address: str
    invoice_tin: str
    invoice_contact: str
    invoice_bank_details: str
    saved: bool = False  # False while the environment defaults are in force

    @property
    def active_taxes(self) -> list[dict]:
        """The tax lines that apply now: none unless tax is switched on."""
        return [dict(t) for t in self.taxes] if self.tax_enabled else []

    def as_dict(self) -> dict:
        d = asdict(self)
        d['taxes'] = [dict(t) for t in self.taxes]
        return d


def from_environment() -> Rules:
    s = settings
    return Rules(
        booking_deposit_percent=s.BOOKING_DEPOSIT_PERCENT,
        unpaid_hold_minutes=s.UNPAID_HOLD_MINUTES,
        free_cancellation_hours=s.FREE_CANCELLATION_HOURS,
        deposit_credit_days=s.DEPOSIT_CREDIT_DAYS,
        max_open_unpaid=getattr(s, 'MAX_OPEN_UNPAID_BOOKINGS', 3),
        reminder_hours_before=getattr(s, 'REMINDER_HOURS_BEFORE', 24),
        studio_open_hour=s.STUDIO_OPEN_HOUR,
        studio_close_hour=s.STUDIO_CLOSE_HOUR,
        pass_paystack_fee=s.PAYSTACK_PASS_FEE_TO_CUSTOMER,
        paystack_fee_basis_points=s.PAYSTACK_FEE_BASIS_POINTS,
        studio_commission_percent=getattr(s, 'STUDIO_COMMISSION_PERCENT', 0),
        marketplace_commission_percent=getattr(s, 'MARKETPLACE_COMMISSION_PERCENT', 10),
        tax_enabled=False,
        taxes=(),
        invoice_business_name=s.INVOICE_BUSINESS_NAME,
        invoice_address=s.INVOICE_ADDRESS,
        invoice_tin=s.INVOICE_TIN,
        invoice_contact=s.INVOICE_CONTACT,
        invoice_bank_details=s.INVOICE_BANK_DETAILS,
    )


def _from_row(values: dict) -> Rules:
    values = dict(values)
    values['taxes'] = tuple(tuple(sorted(t.items())) for t in values.get('taxes') or [])
    return Rules(**values, saved=True)


def _row_values():
    """The saved row's fields, None when there is no row. Cached."""
    hit = cache.get(CACHE_KEY)
    if hit == _NO_ROW:
        return None
    if hit is not None:
        return hit
    from core.models import BusinessSettings
    row = (BusinessSettings.objects.filter(pk=BusinessSettings.SINGLETON_PK)
           .values(*FIELDS).first())
    cache.set(CACHE_KEY, row if row is not None else _NO_ROW, CACHE_SECONDS)
    return row


def rules() -> Rules:
    try:
        row = _row_values()
    except Exception:  # no database (build steps, some unit tests): the environment applies
        row = None
    return _from_row(row) if row is not None else from_environment()


def forget() -> None:
    cache.delete(CACHE_KEY)


def tax_breakdown(total_pesewas: int, taxes: list[dict]) -> tuple[int, list[dict]]:
    """
    Split a tax-INCLUSIVE total into the amount before tax and each tax.

    Every rate applies to the amount before tax (net), so
    total = net x (1 + sum of rates). Pesewas are whole; any rounding
    remainder goes to the net amount, so the parts always add up exactly.
    """
    if not taxes:
        return total_pesewas, []
    bps = sum(int(t['basis_points']) for t in taxes)
    parts = [{'name': t['name'], 'basis_points': int(t['basis_points']),
              'amount_pesewas': (total_pesewas * int(t['basis_points'])) // (10_000 + bps)}
             for t in taxes]
    net = total_pesewas - sum(p['amount_pesewas'] for p in parts)
    return net, parts


def percent_text(basis_points: int) -> str:
    """1500 -> '15%', 250 -> '2.5%'."""
    whole, rest = divmod(int(basis_points), 100)
    if not rest:
        return f'{whole}%'
    return f'{whole}.{rest:02d}'.rstrip('0') + '%'
