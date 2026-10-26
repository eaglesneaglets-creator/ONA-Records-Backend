"""
Invoice and receipt PDFs.

Rendered on request from the frozen Invoice / Receipt rows — so there is no
file to store, nothing to go stale, and the same document always comes back.
ReportLab, pure Python (see requirements.txt for why not WeasyPrint).

Brand: ONA red #C90007 and blue #00108F from the identity guide. The brand
typeface is Lato, but the repo only holds it as web fonts (woff2), which
ReportLab cannot embed; Helvetica stands in until a Lato TTF is added under
apps/payments/fonts/ (tracked in tasks/todo.md).

Output is uncompressed and invariant: identical input gives identical bytes,
and the text is searchable in tests without a PDF parser.
"""

from __future__ import annotations

import io
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

from apps.payments.money import format_ghs

RED = HexColor('#C90007')
BLUE = HexColor('#00108F')
INK = HexColor('#17151F')
DIM = HexColor('#55525F')
RULE = HexColor('#E5E3EA')

TZ = ZoneInfo(settings.TIME_ZONE)
LOGO = Path(__file__).resolve().parent / 'assets' / 'ona-icon.png'

ISSUER = ('ONA Records Creative Services', 'Accra, Ghana')

W, H = A4
LEFT = 20 * mm
RIGHT = W - 20 * mm


def _canvas(buf, title: str) -> Canvas:
    c = Canvas(buf, pagesize=A4, pageCompression=0, invariant=1)
    c.setTitle(title)
    c.setAuthor('ONA Records')
    c.setCreator('ONA Records platform')
    return c


def _header(c: Canvas, kind: str, number: str, issued) -> float:
    # Brand band: the gradient's two ends as solid blocks.
    c.setFillColor(BLUE)
    c.rect(0, H - 10 * mm, W / 2, 10 * mm, stroke=0, fill=1)
    c.setFillColor(RED)
    c.rect(W / 2, H - 10 * mm, W / 2, 10 * mm, stroke=0, fill=1)

    y = H - 28 * mm
    if LOGO.exists():
        c.drawImage(str(LOGO), LEFT, y - 4 * mm, width=14 * mm, height=14 * mm, mask='auto')
    c.setFillColor(INK)
    c.setFont('Helvetica-Bold', 16)
    c.drawString(LEFT + 18 * mm, y + 4 * mm, 'ONA RECORDS')
    c.setFont('Helvetica', 9)
    c.setFillColor(DIM)
    c.drawString(LEFT + 18 * mm, y - 1 * mm, ISSUER[0] + ' · ' + ISSUER[1])

    c.setFillColor(RED)
    c.setFont('Helvetica-Bold', 22)
    c.drawRightString(RIGHT, y + 3 * mm, kind)
    c.setFillColor(INK)
    c.setFont('Helvetica', 10)
    c.drawRightString(RIGHT, y - 3 * mm, number)
    c.setFillColor(DIM)
    c.drawRightString(RIGHT, y - 8 * mm, 'Issued ' + issued.astimezone(TZ).strftime('%d %B %Y'))
    return y - 20 * mm


def _pair(c: Canvas, x: float, y: float, label: str, value: str) -> None:
    c.setFont('Helvetica', 8)
    c.setFillColor(DIM)
    c.drawString(x, y, label.upper())
    c.setFont('Helvetica', 10.5)
    c.setFillColor(INK)
    c.drawString(x, y - 5 * mm, value)


def _rule(c: Canvas, y: float) -> None:
    c.setStrokeColor(RULE)
    c.setLineWidth(0.6)
    c.line(LEFT, y, RIGHT, y)


def _footer(c: Canvas, note: str) -> None:
    c.setFillColor(DIM)
    c.setFont('Helvetica', 8.5)
    c.drawString(LEFT, 20 * mm, note)
    c.drawString(LEFT, 15 * mm, 'Questions about this document? Reply to your booking email '
                                'and quote the reference above.')


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    """Greedy word wrap to a column width, measured in the actual font."""
    lines, current = [], ''
    for word in text.split():
        trial = f'{current} {word}'.strip()
        if current and stringWidth(trial, font, size) > width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines or ['']


def _session_text(booking) -> str:
    start = booking.start_at.astimezone(TZ)
    end = booking.end_at.astimezone(TZ)
    return f"{start.strftime('%a %d %b %Y')}, {start.strftime('%H:%M')}–{end.strftime('%H:%M')}"


def render_invoice(invoice) -> bytes:
    booking = invoice.booking
    buf = io.BytesIO()
    c = _canvas(buf, f'Invoice {invoice.number}')
    # A revised invoice says so, dated when it was revised (Task 15).
    revised = invoice.revision > 1 and invoice.revised_at is not None
    y = _header(c, 'REVISED INVOICE' if revised else 'INVOICE', invoice.number,
                invoice.revised_at if revised else invoice.issued_at)

    _pair(c, LEFT, y, 'Billed to', invoice.customer_name)
    c.setFont('Helvetica', 9.5)
    c.setFillColor(DIM)
    c.drawString(LEFT, y - 10 * mm, invoice.customer_email)
    _pair(c, LEFT + 95 * mm, y, 'Booking reference', booking.reference)
    y -= 22 * mm
    _pair(c, LEFT, y, 'Session (as currently booked)', _session_text(booking))
    _pair(c, LEFT + 95 * mm, y, 'Engineer', booking.engineer.name)
    y -= 18 * mm

    # Line items
    _rule(c, y + 4 * mm)
    c.setFont('Helvetica-Bold', 8.5)
    c.setFillColor(DIM)
    c.drawString(LEFT, y, 'DESCRIPTION')
    c.drawRightString(RIGHT - 60 * mm, y, 'QTY')
    c.drawRightString(RIGHT - 30 * mm, y, 'UNIT')
    c.drawRightString(RIGHT, y, 'AMOUNT')
    y -= 3 * mm
    _rule(c, y)
    c.setFont('Helvetica', 10)
    c.setFillColor(INK)
    desc_width = (RIGHT - 70 * mm) - LEFT
    for line in invoice.lines:
        y -= 7 * mm
        c.drawRightString(RIGHT - 60 * mm, y, str(line['quantity']))
        c.drawRightString(RIGHT - 30 * mm, y, format_ghs(line['unit_pesewas']))
        c.drawRightString(RIGHT, y, format_ghs(line['total_pesewas']))
        # Wrap the description inside its column — a long room or service
        # name used to run straight into the QTY figures.
        for i, text in enumerate(_wrap(line['description'], 'Helvetica', 10, desc_width)):
            if i:
                y -= 5 * mm
            c.drawString(LEFT, y, text)
    y -= 5 * mm
    _rule(c, y)

    y -= 10 * mm
    c.setFont('Helvetica-Bold', 13)
    c.drawRightString(RIGHT - 45 * mm, y, 'Total')
    c.setFillColor(RED)
    c.drawRightString(RIGHT, y, format_ghs(invoice.total_pesewas))
    c.setFillColor(DIM)
    c.setFont('Helvetica', 9)
    paid = booking.transactions.filter(status='succeeded').exists()
    c.drawRightString(RIGHT, y - 6 * mm, 'Paid — held by ONA until your session' if paid
                      else 'Amount due · pay by Mobile Money when prompted')

    _footer(c, 'All amounts in Ghana cedis (GHS). Payments are held by ONA Records, '
               'not passed to anyone else, until your session.')
    c.showPage()
    c.save()
    return buf.getvalue()


def render_receipt(receipt) -> bytes:
    from .models import Transaction

    # Re-read rather than trust receipt.transaction: a caller's in-memory
    # copy may predate the provider reference being saved, and a receipt
    # must show what is actually on record.
    txn = Transaction.objects.select_related('booking__customer', 'booking__room').get(
        pk=receipt.transaction_id)
    booking = txn.booking
    buf = io.BytesIO()
    c = _canvas(buf, f'Receipt {receipt.number}')
    y = _header(c, 'RECEIPT', receipt.number, receipt.issued_at)

    _pair(c, LEFT, y, 'Received from', booking.customer.email)
    _pair(c, LEFT + 95 * mm, y, 'Booking reference', booking.reference)
    y -= 18 * mm
    _pair(c, LEFT, y, 'Paid by', txn.get_provider_display())
    _pair(c, LEFT + 95 * mm, y, 'Payment reference', txn.provider_ref or '—')
    y -= 18 * mm
    _pair(c, LEFT, y, 'For', f'{booking.room.name} · {_session_text(booking)}')
    y -= 20 * mm

    _rule(c, y + 6 * mm)
    c.setFont('Helvetica-Bold', 14)
    c.setFillColor(INK)
    c.drawString(LEFT, y - 2 * mm, 'Amount received')
    c.setFillColor(RED)
    c.drawRightString(RIGHT, y - 2 * mm, format_ghs(receipt.amount_pesewas))
    _rule(c, y - 8 * mm)

    _footer(c, 'Held by ONA Records until your session. If the session cannot go ahead, '
               'you are refunded in full.')
    c.showPage()
    c.save()
    return buf.getvalue()
