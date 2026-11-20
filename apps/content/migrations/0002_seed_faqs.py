"""
Seed the FAQ with the answers the landing page shipped with (Task 16), so
the public page is never empty after deploy and staff edit real policy
rather than starting from nothing. Only runs into an empty table.
"""

from django.db import migrations

FAQS = [
    ("Is an engineer included?",
     "Yes. Every room booking comes with an engineer, and you choose who when you book."),
    ("How do I pay?",
     "In Ghana cedis, through ONA — by Mobile Money, card or bank transfer. You never pay an "
     "engineer or anyone else directly."),
    ("When is my slot really mine?",
     "The moment you confirm. Two bookings can never hold the same room or engineer at the "
     "same time — the system refuses it, not just the form. ONA then confirms the session "
     "with you."),
    ("Can I move or cancel a booking?",
     "Yes. Until your session starts, you can reschedule or cancel from your bookings page, "
     "and the slot is released for someone else."),
    ("What do I get when I book?",
     "A booking reference straight away, an invoice by email, a receipt once your payment is "
     "confirmed, and a reminder the day before your session."),
    ("Can ONA find me a producer or session musician?",
     "Tell us in the notes when you book, and ONA staff can arrange a vetted professional for "
     "your session. Browsing and hiring professionals yourself arrives in a later release."),
]


def seed(apps, schema_editor):
    Faq = apps.get_model('content', 'Faq')
    if Faq.objects.exists():
        return
    for i, (q, a) in enumerate(FAQS):
        Faq.objects.create(question=q, answer=a, sort_order=(i + 1) * 10)


class Migration(migrations.Migration):
    dependencies = [('content', '0001_initial')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
