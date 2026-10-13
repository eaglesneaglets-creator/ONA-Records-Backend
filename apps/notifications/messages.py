"""
What each notification says.

Plain text is the message; the HTML part is the same words with the ONA
brand bar. Every builder returns an Email, nothing is sent here.

Voice (PRODUCT.md): informed, helpful, relaxed, humble. Say what happened,
what happens next, and whether any money moved — in that order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from django.conf import settings
from django.template.loader import render_to_string

from apps.payments.money import format_ghs

TZ = ZoneInfo(settings.TIME_ZONE)


@dataclass
class Email:
    subject: str
    text: str
    html: str
    attachments: list[tuple[str, bytes, str]] = field(default_factory=list)


def _when(booking) -> str:
    s, e = booking.start_at.astimezone(TZ), booking.end_at.astimezone(TZ)
    return f"{s.strftime('%A %d %B %Y')}, {s.strftime('%H:%M')}–{e.strftime('%H:%M')} (Accra time)"


def _name(user) -> str:
    return user.first_name or 'there'


def _bookings_url() -> str:
    return f'{settings.FRONTEND_URL}/customer/bookings'


def _render(template: str, subject: str, ctx: dict) -> Email:
    ctx = {**ctx, 'subject': subject, 'bookings_url': _bookings_url()}
    text = render_to_string(f'notifications/{template}.txt', ctx).strip() + '\n'
    html = render_to_string('notifications/base.html', {
        **ctx, 'body': render_to_string(f'notifications/{template}.txt', ctx).strip(),
    })
    return Email(subject=subject, text=text, html=html)


def booking_context(booking) -> dict:
    return {
        'name': _name(booking.customer),
        'reference': booking.reference,
        'room': booking.room.name,
        'engineer': booking.engineer.name,
        'service': booking.service.name,
        'when': _when(booking),
        'total': format_ghs(booking.total_pesewas),
        'pending': booking.status == 'pending',
    }


def booking_received(booking) -> Email:
    from apps.payments.documents import issue_invoice
    from apps.payments.pdf import render_invoice

    email = _render('booking_received', f'Booking received — {booking.reference}',
                    booking_context(booking))
    invoice = issue_invoice(booking)
    email.attachments.append((f'ONA-invoice-{invoice.number}.pdf', render_invoice(invoice),
                              'application/pdf'))
    return email


def booking_rescheduled(booking) -> Email:
    return _render('booking_rescheduled', f'Your session has moved — {booking.reference}',
                   booking_context(booking))


def booking_cancelled(booking) -> Email:
    return _render('booking_cancelled', f'Booking cancelled — {booking.reference}',
                   booking_context(booking))


def payment_confirmed(booking, receipt) -> Email:
    from apps.payments.pdf import render_receipt

    ctx = {**booking_context(booking), 'amount': format_ghs(receipt.amount_pesewas),
           'receipt_number': receipt.number}
    email = _render('payment_confirmed', f'Payment received — {booking.reference}', ctx)
    email.attachments.append((f'ONA-receipt-{receipt.number}.pdf', render_receipt(receipt),
                              'application/pdf'))
    return email


def reminder(booking) -> Email:
    return _render('reminder', f'Tomorrow at ONA — {booking.reference}',
                   booking_context(booking))


def verify_email(user, link: str) -> Email:
    return _render('verify_email', 'Confirm your email address',
                   {'name': _name(user), 'link': link})


def password_reset(user, link: str) -> Email:
    return _render('password_reset', 'Reset your password', {'name': _name(user), 'link': link})
