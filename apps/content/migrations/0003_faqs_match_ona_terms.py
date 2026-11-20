"""
Bring the seeded FAQ in line with ONA's answers (November 2026): Mobile Money
and bank transfer only, a 50% deposit with Paystack's fee on top, a 1-hour
hold, 48 hours' notice for changes, open 24/7 by booking.

Only answers still exactly as seeded (0002) are rewritten — anything staff
have edited in the console is theirs and is left alone. A new question is
added only if no question with that wording exists.
"""

from django.db import migrations

# question: (answer as seeded in 0002, answer now)
REWRITE = {
    "How do I pay?": (
        "In Ghana cedis, through ONA — by Mobile Money, card or bank transfer. You never pay an "
        "engineer or anyone else directly.",
        "In Ghana cedis, through ONA — by MTN Mobile Money on our secure Paystack checkout, or "
        "by bank transfer to the studio account on your invoice. Online payments include a "
        "small Paystack processing fee, shown before you pay. You never pay an engineer or "
        "anyone else directly."),
    "When is my slot really mine?": (
        "The moment you confirm. Two bookings can never hold the same room or engineer at the "
        "same time — the system refuses it, not just the form. ONA then confirms the session "
        "with you.",
        "When your 50% deposit is paid. Your slot is held for one hour after you book; pay the "
        "deposit in that time and the session is confirmed. Unpaid after an hour, the slot goes "
        "back on the calendar. The balance is due before or on the day of your session."),
    "Can I move or cancel a booking?": (
        "Yes. Until your session starts, you can reschedule or cancel from your bookings page, "
        "and the slot is released for someone else.",
        "Yes, with at least 48 hours' notice, from your bookings page. Deposits are not "
        "refunded: cancel 48 hours or more ahead and your deposit becomes credit towards a new "
        "session within 30 days. With less notice, or if you do not arrive within 30 minutes "
        "of your start time, the deposit is forfeited."),
}

ADD = [
    ("When is the studio open?",
     "24 hours a day, every day of the year — by prior booking only. Walk-ins are not "
     "possible: every session, rehearsal and shoot is booked in advance so the studio and an "
     "engineer are ready for you.", 25),
]


def forwards(apps, schema_editor):
    Faq = apps.get_model('content', 'Faq')
    for question, (seeded, now) in REWRITE.items():
        Faq.objects.filter(question=question, answer=seeded).update(answer=now)
    for question, answer, order in ADD:
        if not Faq.objects.filter(question=question).exists():
            Faq.objects.create(question=question, answer=answer, sort_order=order)


class Migration(migrations.Migration):
    dependencies = [('content', '0002_seed_faqs')]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
