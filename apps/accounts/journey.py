"""
The customer journey — where a customer is, from signing up to their first
session, and what they should do next.

One definition for both sides of the counter:

    customer_journey(user)  -> dict   the customer's "Get started" guide
    stage(user)             -> str    where staff see them on the console

Every step is worked out from the customer's real records, so a step ticks
itself off the moment the thing is done — wherever it was done (online, by
staff at the desk, on the phone). Nothing here stores progress of its own,
apart from the customer choosing to hide the guide.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

# Joined within this long: "new" on the staff console.
NEW_FOR = timedelta(days=14)

# "What brings you to ONA?" — the welcome flow's one question.
INTENTS = {
    'record': 'Record music',
    'mix': 'Mix & master',
    'produce': 'Produce a track',
    'rehearse': 'Rehearse',
    'video': 'Shoot a video',
    'content': 'Make content',
    'browse': 'Just looking',
}
TOURS = ('booking', 'console')

STAGES = {
    'signed_up': 'Signed up',
    'booked': 'Booked',
    'paid': 'Deposit paid',
    'came_in': 'Had a session',
}


def _facts(user) -> dict:
    from apps.payments.models import Transaction, TransactionStatus
    from apps.studio.models import BookingStatus, StudioBooking

    now = timezone.now()
    bookings = StudioBooking.objects.filter(customer=user)
    upcoming = (bookings.filter(status__in=(BookingStatus.PENDING, BookingStatus.CONFIRMED),
                                end_at__gt=now).order_by('start_at').first())
    unpaid = (bookings.filter(status=BookingStatus.PENDING, end_at__gt=now)
              .exclude(transactions__status=TransactionStatus.SUCCEEDED)
              .order_by('created_at').first())
    return {
        'booked': bookings.exists(),
        'paid': Transaction.objects.filter(customer=user,
                                           status=TransactionStatus.SUCCEEDED).exists(),
        'came_in': bookings.filter(status=BookingStatus.COMPLETED).exists()
        or bookings.filter(status=BookingStatus.CONFIRMED, end_at__lte=now).exists(),
        'upcoming': upcoming,
        'unpaid': unpaid,
    }


def stage(user, facts: dict | None = None) -> str:
    f = facts or _facts(user)
    if f['came_in']:
        return 'came_in'
    if f['paid']:
        return 'paid'
    if f['booked']:
        return 'booked'
    return 'signed_up'


def is_new(user) -> bool:
    return user.date_joined >= timezone.now() - NEW_FOR


def customer_journey(user) -> dict:
    from django.conf import settings

    from apps.payments import paystack

    f = _facts(user)
    must_verify = bool(getattr(settings, 'BOOKING_REQUIRES_VERIFIED_EMAIL', True))
    unpaid = f['unpaid']
    upcoming = f['upcoming']

    steps = [
        {'key': 'account', 'title': 'Create your account', 'done': True,
         'body': 'You are in. Your bookings, invoices and receipts live here.'},
        {'key': 'verify_email', 'title': 'Confirm your email', 'done': user.is_email_verified,
         'body': ('We sent you a link. You need it before you can book.' if must_verify
                  else 'We sent you a link, so booking emails reach you.'),
         'action': 'resend_verification'},
        {'key': 'phone', 'title': 'Add your phone number', 'done': bool(user.phone),
         'body': 'So ONA can reach you about your session — the number you pay with by '
                 'Mobile Money works best.',
         'action': 'phone'},
        {'key': 'first_booking', 'title': 'Book your first session', 'done': f['booked'],
         'body': 'Pick the studio, an engineer and what the session is for. Prices are '
                 'shown up front, engineer included.',
         'href': '/customer/book', 'cta': 'Book a session'},
        {'key': 'deposit', 'title': 'Pay your deposit', 'done': f['paid'],
         'body': ('Your slot is held for one hour. Pay the 50% deposit by Mobile Money to '
                  'secure it.' if unpaid and paystack.is_configured()
                  else 'A 50% deposit secures your booking; the balance is due on the day.'),
         'href': '/customer/bookings', 'cta': 'Pay the deposit',
         'urgent': bool(unpaid) and paystack.is_configured()},
        {'key': 'first_session', 'title': 'Come in for your session', 'done': f['came_in'],
         'body': (f'See you on {timezone.localtime(upcoming.start_at):%A %d %B at %H:%M}. '
                  'Bring your files; your engineer will be ready.' if upcoming
                  else 'Your engineer will be ready when you arrive. Walk-ins are not possible '
                       '— every session is booked ahead.'),
         'href': '/customer/bookings', 'cta': 'See your booking'},
    ]
    done = sum(1 for s in steps if s['done'])
    nxt = next((s['key'] for s in steps if not s['done']), None)
    return {
        'steps': steps,
        'done': done,
        'total': len(steps),
        'next': nxt,
        'complete': nxt is None,
        'dismissed': user.journey_dismissed_at is not None,
        'stage': stage(user, f),
        'is_new': is_new(user),
        'intent': user.intent,
        'welcomed': bool((user.onboarding or {}).get('welcomed_at')),
        'tours_seen': list((user.onboarding or {}).get('tours', [])),
    }


def update(user, data) -> None:
    """Apply what the guide, the welcome flow or a tour reports back."""
    from django.utils import timezone as tz

    fields = []
    if 'hidden' in data:
        user.journey_dismissed_at = tz.now() if data['hidden'] else None
        fields.append('journey_dismissed_at')
    if 'intent' in data and data['intent'] in INTENTS:
        user.intent = data['intent']
        fields.append('intent')
    ob = dict(user.onboarding or {})
    if data.get('welcomed'):
        ob.setdefault('welcomed_at', tz.now().isoformat())
    if data.get('tour_seen') in TOURS:
        ob['tours'] = sorted(set(ob.get('tours', [])) | {data['tour_seen']})
    if ob != (user.onboarding or {}):
        user.onboarding = ob
        fields.append('onboarding')
    if fields:
        user.save(update_fields=fields)
