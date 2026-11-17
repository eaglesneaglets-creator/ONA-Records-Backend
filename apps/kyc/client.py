"""
KYC service client — identity checks for ONA users.

The KYC service is a separate application (repo: kyc-service) shared by
every platform. ONA's backend is the only thing that holds ONA's key: the
browser talks to ONA, ONA talks to KYC. A user is known to KYC only by their
ONA user id (the "subject ref"); no email, phone or profile is sent.

Errors map onto what ONA's own API already returns:
    KYC 400 (bad input, or "missing" requirements) -> 400 with the same fields
    KYC 404                                         -> 404
    KYC 409 (not possible in this status)           -> 409
    anything else, or KYC unreachable               -> 503, logged

The key is issued with review rights (create_platform --can-review), so
ONA staff can decide on ONA's own users from the ONA console.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

import requests
from django.conf import settings
from rest_framework.exceptions import APIException, NotFound, ValidationError

logger = logging.getLogger(__name__)

# Uploads are capped at 10 MB by the KYC service; refuse bigger ones here
# rather than send them across the network to be refused there.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
DOCUMENT_KINDS = ('id_front', 'id_back', 'selfie')
# Webhooks older than this are treated as replays.
SIGNATURE_TOLERANCE_SECONDS = 300


class KycUnavailable(APIException):
    status_code = 503
    default_detail = 'Identity checks are not available right now. Please try again shortly.'
    default_code = 'kyc_unavailable'


class KycConflict(APIException):
    status_code = 409
    default_detail = 'That is not possible at this stage of the check.'
    default_code = 'conflict'


def is_configured() -> bool:
    return bool(settings.KYC_API_URL and settings.KYC_API_KEY)


def _body(r: requests.Response):
    try:
        return r.json()
    except ValueError:
        return {}


def _call(method: str, path: str, *, raw: bool = False, **kwargs):
    if not is_configured():
        raise KycUnavailable('Identity checks are not set up yet.')
    url = settings.KYC_API_URL.rstrip('/') + path
    headers = {'Authorization': f'Bearer {settings.KYC_API_KEY}', 'User-Agent': 'ona-records-backend'}
    try:
        r = requests.request(method, url, headers=headers, timeout=20, **kwargs)
    except requests.RequestException as exc:
        logger.warning('KYC %s %s unreachable: %s', method, path, type(exc).__name__)
        raise KycUnavailable() from None

    if 200 <= r.status_code < 300:
        return r if raw else _body(r)
    body = _body(r)
    if r.status_code == 400:
        raise ValidationError(body or 'The identity check refused that.')
    if r.status_code == 404:
        raise NotFound((body or {}).get('detail') or 'Not found.')
    if r.status_code == 409:
        raise KycConflict((body or {}).get('detail') or None)
    # 401/403 mean our key is wrong or revoked; 5xx means KYC is having a bad
    # day. Neither is the user's fault, and neither should show KYC's words.
    logger.error('KYC %s %s answered %s', method, path, r.status_code)
    raise KycUnavailable()


# ── The person's side ────────────────────────────────────────────────────

def start(user_id) -> dict:
    """The open check for this user, created if there is none."""
    return _call('POST', '/v1/verifications/', json={'subject_ref': str(user_id), 'level': 'identity'})


def get(verification_id) -> dict:
    return _call('GET', f'/v1/verifications/{verification_id}/')


def update(verification_id, data: dict) -> dict:
    return _call('PATCH', f'/v1/verifications/{verification_id}/', json=data)


def upload(verification_id, kind: str, file) -> dict:
    file.seek(0)
    return _call('POST', f'/v1/verifications/{verification_id}/documents/', data={'kind': kind},
                 files={'file': (getattr(file, 'name', kind), file, getattr(file, 'content_type', None))})


def submit(verification_id) -> dict:
    return _call('POST', f'/v1/verifications/{verification_id}/submit/')


# ── The reviewer's side ──────────────────────────────────────────────────

def queue(statuses: str, page: int = 1) -> dict:
    return _call('GET', '/v1/review/verifications/', params={'status': statuses, 'page': page})


def review_detail(verification_id, reviewer: str) -> dict:
    return _call('GET', f'/v1/review/verifications/{verification_id}/', params={'reviewer': reviewer})


def document(verification_id, kind: str, reviewer: str) -> tuple[bytes, str]:
    r = _call('GET', f'/v1/review/verifications/{verification_id}/documents/{kind}/',
              params={'reviewer': reviewer}, raw=True)
    return r.content, r.headers.get('Content-Type', 'application/octet-stream')


def start_review(verification_id, reviewer: str) -> dict:
    return _call('POST', f'/v1/review/verifications/{verification_id}/start-review/',
                 json={'reviewer': reviewer})


def decide(verification_id, decision: str, reason: str, reviewer: str) -> dict:
    return _call('POST', f'/v1/review/verifications/{verification_id}/decision/',
                 json={'decision': decision, 'reason': reason, 'reviewer': reviewer})


# ── Webhooks ─────────────────────────────────────────────────────────────

def signature_is_valid(raw_body: bytes, header: str | None, *, now: float | None = None) -> bool:
    """KYC-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>." + body)>."""
    secret = settings.KYC_WEBHOOK_SECRET
    if not secret or not header:
        return False
    try:
        parts = dict(p.split('=', 1) for p in header.split(','))
        ts = int(parts['t'])
        given = parts['v1']
    except (KeyError, ValueError):
        return False
    if abs((now or time.time()) - ts) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f'{ts}.'.encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)
