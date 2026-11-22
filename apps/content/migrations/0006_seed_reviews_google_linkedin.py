"""
Seed the public reviews section, the Google rating summary and ONA's
social profiles (LinkedIn, Facebook and Instagram).

The reviews are real Google reviews of ONA Records (Tema), quoted exactly
as their authors wrote them, including their spelling; a quote cut short on
Google ends at its last full sentence. Authors are shown as first name and
initial. A 4-star review comes early in the homepage's scrolling row, so
the selection does not look curated to perfection. The summary (4.6
from 151 reviews) is as Google showed it on 2026-11-22; staff update it in
the console.

Only empty fields and an empty reviews table are filled, so re-running this
never overwrites what staff have entered since.
"""

from decimal import Decimal

from django.db import migrations

REVIEWS = [
    ('Nii N.', 5,
     'This is a really good recording studio. Music lessons also offered (for young kids as well). '
     'They have some of the finest instrumentalists and you can hire them for gigs (no secular stuff).'),
    ('Richmond D.', 5,
     'A suitable place for recording, overdups, mixing and mastering and rehearsals\n'
     'Students can learn instruments from here too'),
    ('Johnson A.', 5,
     'Ona Records is great!!! When it comes to recording a new song ,be it single or an album,'
     'trust me Ona Records is the place to be'),
    ('Emmanuel D.', 4, 'Cool serene place in Tema for recording and rehearsals'),
    ('Paul O.', 5,
     'You can do all your recordings here.\n'
     'Great music teachers who patiently and painstakingly teach you how to play any musical instrument'),
    ('Nii A.', 5,
     'Spacious for choir and band.... Can have your live recordings and rehearsals. '
     'Also have a school to improve your skills'),
    ('Kabiekyile J.', 5,
     'This place is good for all your recordings,you have to record all your music,video and more here,thank you'),
    ('David O.', 5,
     'If you want to learn a musical instrument, or want to record a song, then you can go no where else than ona records'),
]

GOOGLE_URL = 'https://www.google.com/maps/search/?api=1&query=ONA+Records+Tema'
SOCIAL = {
    'linkedin_url': 'https://www.linkedin.com/company/ona-records/',
    'facebook_url': 'https://www.facebook.com/onastudiosgh/',
    'instagram_url': 'https://www.instagram.com/onaplay/',
}


def seed(apps, schema_editor):
    Testimonial = apps.get_model('content', 'Testimonial')
    SiteContent = apps.get_model('content', 'SiteContent')
    ContactDetails = apps.get_model('content', 'ContactDetails')

    if not Testimonial.objects.exists():
        Testimonial.objects.bulk_create([
            Testimonial(author=a, rating=r, quote=q, source='google', sort_order=(i + 1) * 10)
            for i, (a, r, q) in enumerate(REVIEWS)
        ])

    site, _ = SiteContent.objects.get_or_create(pk=1)
    if site.google_rating is None and site.google_review_count is None:
        site.google_rating = Decimal('4.6')
        site.google_review_count = 151
        site.google_reviews_url = site.google_reviews_url or GOOGLE_URL
        site.save()

    contact, _ = ContactDetails.objects.get_or_create(pk=1)
    blank = [f for f in SOCIAL if not getattr(contact, f)]
    for f in blank:
        setattr(contact, f, SOCIAL[f])
    if blank:
        contact.save()


def unseed(apps, schema_editor):
    Testimonial = apps.get_model('content', 'Testimonial')
    for author, _, quote in REVIEWS:
        Testimonial.objects.filter(author=author, quote=quote).delete()


class Migration(migrations.Migration):
    dependencies = [('content', '0005_site_media_gallery_reviews')]
    operations = [migrations.RunPython(seed, unseed)]
