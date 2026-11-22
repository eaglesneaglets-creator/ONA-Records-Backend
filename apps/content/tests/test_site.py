"""
The public website's content: the media library, the homepage hero, the
gallery, the quoted reviews, the Google summary and ONA's social links.
"""

import io
from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image
from rest_framework.test import APIClient

from apps.content.models import ContactDetails, GalleryImage, MediaAsset, SiteContent, Testimonial
from core.models import AuditLog

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(django_user_model):
    c = APIClient()
    c.force_authenticate(django_user_model.objects.create_user(
        email='staff@x.test', password='p-Ass-1', role='admin'))
    return c


@pytest.fixture
def customer(django_user_model):
    c = APIClient()
    c.force_authenticate(django_user_model.objects.create_user(
        email='c@x.test', password='p-Ass-1', role='customer'))
    return c


@pytest.fixture
def bucket():
    """The bucket, faked: records what was stored, serves it back."""
    stored = {}

    def upload(file, *, context):
        data = file.read()
        key = f'ona/site/{len(stored)}.webp'
        stored[key] = (data, file.content_type, context)
        return {'key': key, 'bytes': len(data), 'content_type': file.content_type}

    def open_stream(key):
        if key not in stored:
            return None
        data, ctype, _ = stored[key]
        return io.BytesIO(data), ctype, len(data)

    with mock.patch('apps.content.media.storage.upload', side_effect=upload), \
            mock.patch('apps.content.views.storage.open_stream', side_effect=open_stream), \
            mock.patch('apps.content.views.storage.delete', return_value=True) as delete:
        yield {'stored': stored, 'delete': delete}


def photo(width=3000, height=1000, fmt='JPEG', gps=True) -> SimpleUploadedFile:
    """A camera-like photo; with gps, it carries an EXIF GPS block as phones do."""
    img = Image.new('RGB', (width, height), (200, 30, 30))
    out = io.BytesIO()
    kwargs = {}
    if gps and fmt == 'JPEG':
        exif = Image.Exif()
        exif[0x8825] = {1: 'N', 2: (5.0, 40.0, 12.0)}  # GPSInfo: 5°40'12"N, near Tema
        kwargs['exif'] = exif.tobytes()
    img.save(out, fmt, **kwargs)
    mime = {'JPEG': 'image/jpeg', 'PNG': 'image/png'}[fmt]
    return SimpleUploadedFile(f'IMG_0001.{fmt.lower()}', out.getvalue(), content_type=mime)


def upload(client, **extra):
    return client.post(reverse('adminapi:media-list'), {'file': photo(**extra), 'alt_text': ' Live room '},
                       format='multipart')


class TestSeeded:
    def test_reviews_google_summary_and_social_profiles_are_seeded(self, client):
        res = client.get(reverse('content:site'))
        assert res.status_code == 200
        body = res.json()
        assert len(body['reviews']) == 8
        assert body['reviews'][0]['author'] == 'Nii N.'
        # A 4-star review comes early in the homepage's scrolling row.
        assert 4 in [r['rating'] for r in body['reviews'][:4]]
        assert body['google'] == {'rating': '4.6', 'count': 151, 'url': mock.ANY}
        assert body['hero']['image'] is None and body['hero']['title'] == ''
        contact = client.get(reverse('content:contact')).json()
        assert contact['linkedin_url'] == 'https://www.linkedin.com/company/ona-records/'
        assert contact['facebook_url'] == 'https://www.facebook.com/onastudiosgh/'
        assert contact['instagram_url'] == 'https://www.instagram.com/onaplay/'


class TestMediaLibrary:
    def test_upload_is_reencoded_to_webp_resized_and_stripped(self, staff, bucket):
        res = upload(staff)
        assert res.status_code == 201, res.content
        asset = MediaAsset.objects.get()
        assert (asset.width, asset.height) == (2400, 800)  # long edge capped
        assert asset.alt_text == 'Live room'
        data, ctype, context = bucket['stored'][asset.key]
        assert ctype == 'image/webp' and context == 'site_media'
        stored = Image.open(io.BytesIO(data))
        assert stored.format == 'WEBP'
        assert 0x8825 not in stored.getexif()  # the GPS position is gone
        assert AuditLog.objects.filter(action='content.media_uploaded').exists()

    def test_something_that_is_not_an_image_is_refused(self, staff, bucket):
        fake = SimpleUploadedFile('x.jpg', b'not really a jpeg', content_type='image/jpeg')
        res = staff.post(reverse('adminapi:media-list'), {'file': fake}, format='multipart')
        assert res.status_code == 400
        assert not MediaAsset.objects.exists() and not bucket['stored']

    def test_only_images_are_accepted(self, staff, bucket):
        pdf = SimpleUploadedFile('x.pdf', b'%PDF-1.4', content_type='application/pdf')
        assert staff.post(reverse('adminapi:media-list'), {'file': pdf}, format='multipart').status_code == 400

    def test_customers_cannot_touch_the_library(self, customer, bucket):
        assert upload(customer).status_code == 403
        assert customer.get(reverse('adminapi:media-list')).status_code == 403

    def test_the_public_url_serves_the_bytes_and_caches_for_good(self, staff, client, bucket):
        asset_id = upload(staff).json()['id']
        res = client.get(reverse('content:media', args=[asset_id]))
        assert res.status_code == 200
        assert res['Content-Type'] == 'image/webp'
        assert 'immutable' in res['Cache-Control']
        assert Image.open(io.BytesIO(b''.join(res.streaming_content))).format == 'WEBP'

    def test_an_unknown_image_is_a_404(self, client, bucket):
        assert client.get(reverse('content:media', args=['00000000-0000-0000-0000-000000000000'])).status_code == 404

    def test_an_image_the_site_shows_cannot_be_deleted(self, staff, bucket):
        asset_id = upload(staff).json()['id']
        staff.patch(reverse('adminapi:site'), {'hero_image': asset_id}, format='json')
        res = staff.delete(reverse('adminapi:media-detail', args=[asset_id]))
        assert res.status_code == 409
        # In the API's error envelope, so the console shows the reason, not a generic "conflict".
        assert res.json()['error']['code'] == 'in_use'
        assert 'homepage hero' in res.json()['error']['detail']['detail']
        bucket['delete'].assert_not_called()

    def test_an_unused_image_is_deleted_from_the_bucket_too(self, staff, bucket):
        asset = upload(staff).json()
        assert staff.delete(reverse('adminapi:media-detail', args=[asset['id']])).status_code == 204
        assert not MediaAsset.objects.exists()
        bucket['delete'].assert_called_once()


class TestHero:
    def test_admin_sets_the_hero_and_the_change_is_audited(self, staff, client, bucket):
        asset_id = upload(staff).json()['id']
        res = staff.patch(reverse('adminapi:site'), {
            'hero_image': asset_id, 'hero_title': ' Record in Tema. \n Keep your money safe. ', 'hero_focus': 40,
        }, format='json')
        assert res.status_code == 200, res.content
        hero = client.get(reverse('content:site')).json()['hero']
        assert hero['title'] == 'Record in Tema.\nKeep your money safe.'
        assert hero['image']['id'] == asset_id and hero['image']['src'].endswith(f'/media/{asset_id}/')
        assert hero['focus'] == 40
        log = AuditLog.objects.get(action='content.site_updated')
        assert set(log.metadata['changes']) == {'hero_image', 'hero_title', 'hero_focus'}

    def test_the_headline_is_kept_to_three_lines(self, staff):
        res = staff.patch(reverse('adminapi:site'), {'hero_title': 'a\nb\nc\nd'}, format='json')
        assert res.status_code == 400

    def test_a_rating_outside_one_to_five_is_refused(self, staff):
        assert staff.patch(reverse('adminapi:site'), {'google_rating': '6.0'}, format='json').status_code == 400


class TestGalleryAndReviews:
    def test_only_published_photos_reach_the_public_gallery(self, staff, client, bucket):
        a = upload(staff).json()['id']
        b = upload(staff).json()['id']
        staff.post(reverse('adminapi:gallery-list'), {'media': a, 'caption': 'Live room', 'sort_order': 2}, format='json')
        staff.post(reverse('adminapi:gallery-list'), {'media': b, 'is_published': False}, format='json')
        gallery = client.get(reverse('content:site')).json()['gallery']
        assert [g['caption'] for g in gallery] == ['Live room']
        assert gallery[0]['image']['width'] == 2400

    def test_a_review_edit_is_audited_and_unpublishing_hides_it(self, staff, client):
        t = Testimonial.objects.get(author='Nii N.')
        res = staff.patch(reverse('adminapi:testimonial-detail', args=[t.pk]), {'is_published': False}, format='json')
        assert res.status_code == 200
        assert 'Nii N.' not in [r['author'] for r in client.get(reverse('content:site')).json()['reviews']]
        assert AuditLog.objects.filter(action='content.testimonial_updated').exists()

    def test_a_review_needs_text_and_an_author(self, staff):
        res = staff.post(reverse('adminapi:testimonial-list'), {'quote': '  ', 'author': ''}, format='json')
        assert res.status_code == 400
        assert 'quote' in res.content.decode() and 'author' in res.content.decode()


class TestSocialLinks:
    def test_a_link_is_cleaned_of_tracking(self, staff, client):
        res = staff.patch(reverse('adminapi:contact'), {
            'instagram_url': 'https://www.instagram.com/onarecords/?igsh=abc123',
        }, format='json')
        assert res.status_code == 200, res.content
        assert ContactDetails.load().instagram_url == 'https://www.instagram.com/onarecords/'

    @pytest.mark.parametrize('url', [
        'https://evil.example/instagram.com',
        'http://www.instagram.com/onarecords',
        'https://instagram.com.evil.example/x',
    ])
    def test_a_link_to_another_site_is_refused(self, staff, url):
        assert staff.patch(reverse('adminapi:contact'), {'instagram_url': url}, format='json').status_code == 400

    def test_facebook_as_copied_from_a_browser_is_accepted(self, staff):
        res = staff.patch(reverse('adminapi:contact'), {
            'facebook_url': 'https://web.facebook.com/onastudiosgh/?_rdc=1&_rdr#',
        }, format='json')
        assert res.status_code == 200, res.content
        assert ContactDetails.load().facebook_url == 'https://web.facebook.com/onastudiosgh/'

    def test_a_link_can_be_cleared(self, staff):
        assert staff.patch(reverse('adminapi:contact'), {'linkedin_url': ''}, format='json').status_code == 200
        assert ContactDetails.load().linkedin_url == ''


def test_the_site_content_is_one_row():
    assert SiteContent.load().pk == SiteContent.load().pk == SiteContent.SINGLETON_PK
    assert GalleryImage.objects.count() == 0
