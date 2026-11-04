"""
What a booking costs — one definition for the customer API, the staff
console and the invoice.

ONA prices by SERVICE, not by room (their answers, 2026-11): "Studio One —
flexible pricing based on service, engineer included". A service is priced
in one of four units:

    hour    Rehearsal Space GHS 150/hour          price × hours
    day     Full Day Studio Pass GHS 1,000/8h     price × days (whole 8-hour days)
    song    Digital Production GHS 4,000/song     price × songs
    track   Mixing & Mastering GHS 1,000/track    price × tracks

For songs and tracks the customer also books the studio time they need; that
time reserves the room and the engineer but is not charged on top — the
per-song price is the price.

A room MAY still carry an hourly hire rate, added on top for every hour.
ONA's room has none; the option stays because "room hire + service" is how
most studios price, and removing it would be the costlier change to undo.

Integer pesewas throughout. Nothing here touches the database.
"""

from __future__ import annotations

from dataclasses import dataclass

DAY_HOURS = 8
MAX_QUANTITY = 20


class PricingError(ValueError):
    """The request does not fit the service's unit. Message is customer-safe."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class Quote:
    hours: int
    quantity: int           # songs or tracks; 1 for hourly and daily services
    service_pesewas: int
    room_pesewas: int

    @property
    def total_pesewas(self) -> int:
        return self.service_pesewas + self.room_pesewas


def quote(room, service, hours: int, quantity: int = 1) -> Quote:
    from .models import PriceUnit

    if type(hours) is not int or hours < 1:
        raise PricingError('hours', 'A session is at least one hour.')
    unit, price = service.price_unit, service.base_price_pesewas

    if unit == PriceUnit.HOUR:
        quantity, service_part = 1, price * hours
    elif unit == PriceUnit.DAY:
        if hours % DAY_HOURS:
            raise PricingError('hours', f'{service.name} is booked in whole days of '
                                        f'{DAY_HOURS} hours.')
        quantity, service_part = 1, price * (hours // DAY_HOURS)
    else:  # SONG, TRACK
        if type(quantity) is not int or not 1 <= quantity <= MAX_QUANTITY:
            raise PricingError('quantity', f'Choose between 1 and {MAX_QUANTITY} '
                                           f'{service.get_price_unit_display()}s.')
        service_part = price * quantity

    room_part = (room.hourly_rate_pesewas or 0) * hours
    return Quote(hours=hours, quantity=quantity, service_pesewas=service_part,
                 room_pesewas=room_part)


def invoice_lines(room, service, q: Quote) -> list[dict]:
    """Line items for a quote. Lines sum to q.total_pesewas exactly."""
    from .models import PriceUnit

    lines = []
    if q.service_pesewas:
        unit = service.price_unit
        if unit == PriceUnit.HOUR:
            desc, qty = f'{service.name} · {room.name} · engineer included, per hour', q.hours
        elif unit == PriceUnit.DAY:
            desc, qty = (f'{service.name} · {room.name} · {DAY_HOURS}-hour day, engineer '
                         f'included'), q.hours // DAY_HOURS
        else:
            desc, qty = (f'{service.name} · per {service.get_price_unit_display()} · '
                         f'{q.hours} h studio time included'), q.quantity
        lines.append({'description': desc, 'quantity': qty,
                      'unit_pesewas': service.base_price_pesewas,
                      'total_pesewas': q.service_pesewas})
    if q.room_pesewas:
        desc = (f'{room.name} hire, per hour' if q.service_pesewas
                else f'{room.name} · {service.name} · engineer included, per hour')
        lines.append({'description': desc, 'quantity': q.hours,
                      'unit_pesewas': room.hourly_rate_pesewas, 'total_pesewas': q.room_pesewas})
    return lines
