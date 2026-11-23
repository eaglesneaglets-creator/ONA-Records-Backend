"""
Enhancing (AI upscaling) a small website photo with Real-ESRGAN on Replicate.

Replicate is faked at the HTTP layer (requests), so these tests cover what
the code sends, what it accepts back, and what the site shows afterwards.
"""

import io
from datetime import timedelta
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.content import enhance
from apps.content.models import GalleryImage, MediaAsset, SiteContent
from apps.content.tasks import enhance_media
from core.models import AuditLog

from .test_site import bucket, customer, staff, upload  # noqa: F401 — fixtures and helper

pytestmark = pytest.mark.django_db

TOKEN = 'r8_test_token'
VERSION = 'a' * 64
OUT_URL = 'https://replicate.delivery/xezq/abc/out.png'


def png(width, height) -> bytes:
    out = io.BytesIO()
    Image.new('RGB', (width, height), (20, 20, 160)).save(out, 'PNG')
    return out.getvalue()


class Resp:
    def __init__(self, status=200, body=None, data=b'', ctype='application/json'):
        self.status_code, self._body, self._data = status, body or {}, data
        self.headers = {'Content-Type': ctype}
        self.text = str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(self.status_code)

    def iter_content(self, n):
        for i in range(0, len(self._data), n):
            yield self._data[i:i + n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def replicate(settings):
    """Replicate's API, faked. `calls` records (method, url, json, headers)."""
    settings.REPLICATE_API_TOKEN = TOKEN
    settings.REPLICATE_UPSCALE_VERSION = ''
    state = {'calls': [], 'predict': [{'id': 'p1', 'status': 'succeeded', 'output': OUT_URL}],
             'result': Resp(data=png(1200, 900), ctype='image/png')}

    def request(method, url, **kw):
        state['calls'].append((method, url, kw.get('json'), kw.get('headers')))
        if url.endswith('/models/nightmareai/real-esrgan'):
            return Resp(body={'latest_version': {'id': VERSION}})
        return Resp(body=state['predict'].pop(0))

    def get(url, **kw):
        state['downloaded'] = url
        return state['result']

    with mock.patch('apps.content.enhance.requests.request', side_effect=request), \
            mock.patch('apps.content.enhance.requests.get', side_effect=get), \
            mock.patch('apps.content.enhance.storage.url_for', return_value='https://bucket.test/signed?x=1'), \
            mock.patch('apps.content.enhance.time.sleep'):
        from django.core.cache import cache
        cache.clear()
        yield state


def small_asset(staff, w=600, h=450):  # noqa: F811
    res = upload(staff, width=w, height=h, gps=False)
    assert res.status_code == 201, res.content
    return MediaAsset.objects.get(pk=res.json()['id'])


def enhance_url(asset):
    return reverse('adminapi:media-enhance', args=[asset.pk])


class TestRequest:
    def test_library_says_which_photos_are_small(self, staff, bucket):  # noqa: F811
        small_asset(staff)
        upload(staff, width=3000, height=2000, gps=False)
        rows = staff.get(reverse('adminapi:media-list')).json()
        rows = rows.get('results', rows)
        assert sorted(r['is_small'] for r in rows) == [False, True]
        assert {r['enhance_status'] for r in rows} == {''}

    def test_not_set_up_is_a_503_in_the_error_envelope(self, staff, bucket, settings):  # noqa: F811
        settings.REPLICATE_API_TOKEN = ''
        res = staff.post(enhance_url(small_asset(staff)))
        assert res.status_code == 503
        assert res.json()['error']['code'] == 'enhance_unavailable'

    def test_a_large_photo_is_refused(self, staff, bucket, replicate):  # noqa: F811
        res = upload(staff, width=3000, height=2000, gps=False)
        big = MediaAsset.objects.get(pk=res.json()['id'])
        res = staff.post(enhance_url(big))
        assert res.status_code == 400
        assert res.json()['error']['code'] == 'already_sharp'

    def test_queues_the_worker_and_marks_the_photo_pending(self, staff, bucket, replicate,  # noqa: F811
                                                         django_capture_on_commit_callbacks):
        asset = small_asset(staff)
        with mock.patch('apps.content.tasks.enhance_media.delay') as delay, \
                django_capture_on_commit_callbacks(execute=True):
            res = staff.post(enhance_url(asset))
        assert res.status_code == 202
        assert res.json()['enhance_status'] == 'pending'
        delay.assert_called_once_with(str(asset.pk), mock.ANY)
        assert AuditLog.objects.filter(action='content.media_enhance_requested').count() == 1

    def test_a_second_click_while_pending_is_refused_until_the_job_looks_lost(self, staff, bucket,  # noqa: F811
                                                                             replicate):
        asset = small_asset(staff)
        with mock.patch('apps.content.tasks.enhance_media.delay'):
            assert staff.post(enhance_url(asset)).status_code == 202
            res = staff.post(enhance_url(asset))
            assert res.status_code == 409 and res.json()['error']['code'] == 'enhance_pending'
            MediaAsset.objects.filter(pk=asset.pk).update(
                enhance_requested_at=timezone.now() - timedelta(minutes=20))
            assert staff.post(enhance_url(asset)).status_code == 202

    def test_daily_limit_caps_paid_calls(self, staff, bucket, replicate, settings):  # noqa: F811
        settings.MEDIA_ENHANCE_DAILY_LIMIT = 1
        a, b = small_asset(staff), small_asset(staff)
        with mock.patch('apps.content.tasks.enhance_media.delay'):
            assert staff.post(enhance_url(a)).status_code == 202
            res = staff.post(enhance_url(b))
        assert res.status_code == 429 and res.json()['error']['code'] == 'enhance_limit'

    def test_customers_cannot_enhance(self, customer, staff, bucket, replicate):  # noqa: F811
        assert customer.post(enhance_url(small_asset(staff))).status_code == 403


class TestWorker:
    def pending(self, staff, **kw):  # noqa: F811
        asset = small_asset(staff, **kw)
        asset.enhance_status = MediaAsset.Enhance.PENDING
        asset.save()
        return asset

    def test_upscales_and_puts_the_new_photo_where_the_old_one_was(self, staff, bucket, replicate):  # noqa: F811
        asset = self.pending(staff)
        site = SiteContent.load() if hasattr(SiteContent, 'load') else SiteContent.objects.get_or_create(pk=1)[0]
        site.hero_image = asset
        site.save()
        item = GalleryImage.objects.create(media=asset, caption='Live room')

        assert enhance_media(str(asset.pk)) == 'done'

        new = MediaAsset.objects.get(enhanced_from=asset)
        assert (new.width, new.height) == (1200, 900)
        assert new.content_type == 'image/webp' and new.alt_text == asset.alt_text
        assert SiteContent.objects.get(pk=site.pk).hero_image == new
        assert GalleryImage.objects.get(pk=item.pk).media == new
        asset.refresh_from_db()
        assert asset.enhance_status == 'done' and asset.enhance_error == ''
        # The request Replicate got: the latest version, a signed link, 4x for a 600 px photo, faces untouched.
        post = next(c for c in replicate['calls'] if c[0] == 'post')
        assert post[2] == {'version': VERSION, 'input': {
            'image': 'https://bucket.test/signed?x=1', 'scale': 4, 'face_enhance': False}}
        assert post[3]['Authorization'] == f'Bearer {TOKEN}' and post[3]['Prefer'] == 'wait=60'
        assert AuditLog.objects.filter(action='content.media_enhanced').count() == 1

    def test_waits_for_a_slow_prediction(self, staff, bucket, replicate):  # noqa: F811
        replicate['predict'] = [{'id': 'p9', 'status': 'processing'},
                                {'id': 'p9', 'status': 'succeeded', 'output': [OUT_URL]}]
        asset = self.pending(staff)
        assert enhance_media(str(asset.pk)) == 'done'
        assert replicate['calls'][-1][:2] == ('get', 'https://api.replicate.com/v1/predictions/p9')

    def test_refuses_output_from_anywhere_but_replicate(self, staff, bucket, replicate):  # noqa: F811
        replicate['predict'] = [{'id': 'p1', 'status': 'succeeded', 'output': 'http://169.254.169.254/latest'}]
        asset = self.pending(staff)
        assert enhance_media(str(asset.pk)) == 'failed'
        asset.refresh_from_db()
        assert asset.enhance_status == 'failed'
        assert 'unexpected' in asset.enhance_error
        assert 'downloaded' not in replicate
        assert not MediaAsset.objects.filter(enhanced_from=asset).exists()

    def test_a_failed_prediction_leaves_the_site_as_it_was(self, staff, bucket, replicate):  # noqa: F811
        replicate['predict'] = [{'id': 'p1', 'status': 'failed', 'error': 'CUDA out of memory'}]
        asset = self.pending(staff)
        item = GalleryImage.objects.create(media=asset)
        assert enhance_media(str(asset.pk)) == 'failed'
        assert GalleryImage.objects.get(pk=item.pk).media == asset
        asset.refresh_from_db()
        assert asset.enhance_error == 'The enhancer could not process this photo.'

    def test_an_unexpected_crash_still_ends_the_pending_state(self, staff, bucket, replicate):  # noqa: F811
        asset = self.pending(staff)
        with mock.patch('apps.content.tasks.upscale', side_effect=RuntimeError('boom')):
            assert enhance_media(str(asset.pk)) == 'failed'
        asset.refresh_from_db()
        assert asset.enhance_status == 'failed'

    def test_a_duplicate_delivery_does_nothing(self, staff, bucket, replicate):  # noqa: F811
        asset = small_asset(staff)  # not pending
        assert enhance_media(str(asset.pk)) == 'not-pending'
        assert replicate['calls'] == []


@pytest.mark.parametrize('size,scale', [((300, 200), 4), ((600, 450), 4), ((900, 600), 3), ((1500, 1000), 2)])
def test_scale_reaches_the_stored_size_without_going_past_4x(size, scale):
    assert enhance.scale_for(*size) == scale
