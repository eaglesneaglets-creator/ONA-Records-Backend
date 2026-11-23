"""
Enhancing a small website photo: AI upscaling with Real-ESRGAN on Replicate.

An admin clicks Enhance on a photo under MediaAsset.SMALL_EDGE pixels; the
worker (apps/content/tasks.py) calls upscale() here. Replicate reads the
photo from a signed bucket link that expires in 15 minutes, upscales it 2-4x,
and returns a link to the result, which goes through the same re-encoding as
an upload (WebP, at most media.MAX_EDGE px, no metadata).

What upscaling can and cannot do: it sharpens edges and removes blur and
JPEG blocks convincingly, but the extra detail is the model's best guess,
not what the camera saw. Face enhancement (GFPGAN) stays off: it repaints
faces, and a real customer's face must not be invented.
"""

import logging
import math
import time
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.exceptions import ValidationError

from core import storage

from .media import ACCEPTED, MAX_EDGE, process_upload

logger = logging.getLogger(__name__)

API = 'https://api.replicate.com/v1'
#: Replicate serves prediction output only from these hosts. Anything else
#: in a response is refused rather than downloaded.
OUTPUT_HOST = 'replicate.delivery'
#: An upscaled PNG before re-encoding: 2400 x 2400 x 4 bytes is ~23 MB raw.
MAX_RESULT_BYTES = 40 * 1024 * 1024
#: Longest the worker waits for one photo; Real-ESRGAN usually takes 5-20 s.
TIMEOUT_SECONDS = 240
POLL_SECONDS = 3
TERMINAL = {'succeeded', 'failed', 'canceled'}


class EnhanceError(Exception):
    """A failure to show the admin, in words they can act on."""


def configured() -> bool:
    return bool(settings.REPLICATE_API_TOKEN)


def scale_for(width: int, height: int) -> int:
    """Enough to reach MAX_EDGE on the long side, between 2x and 4x (the model's native range)."""
    return max(2, min(4, math.ceil(MAX_EDGE / max(width, height, 1))))


def _headers(**extra) -> dict:
    return {'Authorization': f'Bearer {settings.REPLICATE_API_TOKEN}', 'Content-Type': 'application/json', **extra}


def _call(method: str, url: str, **kw) -> dict:
    try:
        r = requests.request(method, url, timeout=kw.pop('timeout', 30), **kw)
    except requests.RequestException:
        logger.exception('Replicate request failed: %s %s', method, url)
        raise EnhanceError('Could not reach the enhancer. Try again in a few minutes.') from None
    if r.status_code in (401, 403):
        raise EnhanceError('The enhancer refused the API token. Check REPLICATE_API_TOKEN.')
    if r.status_code == 402:
        raise EnhanceError('The enhancer account is out of credit. Add billing on Replicate.')
    if r.status_code == 429:
        raise EnhanceError('The enhancer is busy. Try again in a minute.')
    if r.status_code >= 400:
        logger.error('Replicate %s %s -> %s %s', method, url, r.status_code, r.text[:300])
        raise EnhanceError('The enhancer could not take this photo.')
    return r.json()


def _version() -> str:
    if settings.REPLICATE_UPSCALE_VERSION:
        return settings.REPLICATE_UPSCALE_VERSION
    key = f'replicate:latest:{settings.REPLICATE_UPSCALE_MODEL}'
    version = cache.get(key)
    if not version:
        model = _call('get', f'{API}/models/{settings.REPLICATE_UPSCALE_MODEL}', headers=_headers())
        version = (model.get('latest_version') or {}).get('id')
        if not version:
            raise EnhanceError('The enhancer model has no published version.')
        cache.set(key, version, 6 * 3600)
    return version


def _download(url: str) -> tuple[bytes, str]:
    """The result file, from Replicate's delivery host only, size-capped."""
    u = urlparse(str(url or ''))
    host = (u.hostname or '').lower()
    if u.scheme != 'https' or not (host == OUTPUT_HOST or host.endswith('.' + OUTPUT_HOST)):
        logger.error('Replicate returned an unexpected output URL: %r', url)
        raise EnhanceError('The enhancer returned something unexpected.')
    try:
        with requests.get(url, stream=True, timeout=60, allow_redirects=False) as r:
            r.raise_for_status()
            chunks, size = [], 0
            for chunk in r.iter_content(64 * 1024):
                size += len(chunk)
                if size > MAX_RESULT_BYTES:
                    raise EnhanceError('The enhanced photo was too large to keep.')
                chunks.append(chunk)
            ctype = (r.headers.get('Content-Type') or '').split(';')[0].strip().lower()
    except requests.RequestException:
        logger.exception('Could not download the enhanced photo')
        raise EnhanceError('Could not fetch the enhanced photo. Try again.') from None
    # Pillow checks the bytes in process_upload whatever the header says.
    return b''.join(chunks), ctype if ctype in ACCEPTED else 'image/png'


def upscale(asset) -> dict:
    """Upscale asset's photo and store the result; returns process_upload's dict."""
    if not configured():
        raise EnhanceError('Photo enhancing is not set up (REPLICATE_API_TOKEN).')
    image_url = storage.url_for(asset.key, expires=900)
    if not image_url:
        raise EnhanceError('The photo could not be read from storage.')
    body = {
        'version': _version(),
        'input': {'image': image_url, 'scale': scale_for(asset.width, asset.height), 'face_enhance': False},
    }
    # Prefer: wait holds the request open until the result is ready (up to 60 s).
    pred = _call('post', f'{API}/predictions', json=body, headers=_headers(Prefer='wait=60'), timeout=75)
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while pred.get('status') not in TERMINAL:
        pid = str(pred.get('id') or '')
        if not pid.isalnum():
            raise EnhanceError('The enhancer returned something unexpected.')
        if time.monotonic() > deadline:
            raise EnhanceError('Enhancing took too long. Try again later.')
        time.sleep(POLL_SECONDS)
        pred = _call('get', f'{API}/predictions/{pid}', headers=_headers())
    if pred.get('status') != 'succeeded':
        logger.warning('Replicate prediction %s ended %s: %s', pred.get('id'), pred.get('status'), pred.get('error'))
        raise EnhanceError('The enhancer could not process this photo.')
    output = pred.get('output')
    if isinstance(output, list):
        output = output[0] if output else None
    data, ctype = _download(output)
    try:
        return process_upload(SimpleUploadedFile('enhanced', data, content_type=ctype), max_bytes=MAX_RESULT_BYTES)
    except ValidationError:
        raise EnhanceError('The enhanced photo could not be read.') from None
