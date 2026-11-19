"""
Booking engine client: the shared Booking API (repo: BookingAPI).

ONA's backend is the only thing holding ONA's key; the browser never talks
to the engine. Every call is scoped to ONA by that key, so nothing here can
see or touch another platform's resources or bookings.

Failures are raised as BookingEngineUnavailable (network, 5xx, a bad key) or
BookingEngineRefused (the engine said no: 400/404/409, with its `code`).
Sync code catches both; nothing customer-facing depends on the engine yet.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)
TIMEOUT = 20


class BookingEngineUnavailable(Exception):
    """The engine could not be reached or failed; worth retrying later."""


class BookingEngineRefused(Exception):
    """The engine answered with a 4xx. `status`, `code` and `body` say why."""

    def __init__(self, status: int, body: dict):
        self.status, self.body = status, body or {}
        self.code = self.body.get('code', '')
        details = self.body.get('details') or self.body.get('fields')
        super().__init__(f'{status} {self.code}: {self.body.get("detail", "")}'.strip()
                         + (f' {details}' if details else ''))


def is_configured() -> bool:
    return bool(settings.BOOKING_API_URL and settings.BOOKING_API_KEY)


def _call(method: str, path: str, **kwargs):
    if not is_configured():
        raise BookingEngineUnavailable('The booking engine is not set up (BOOKING_API_URL / BOOKING_API_KEY).')
    url = settings.BOOKING_API_URL.rstrip('/') + path
    headers = {'Authorization': f'Bearer {settings.BOOKING_API_KEY}', 'User-Agent': 'ona-records-backend',
               **kwargs.pop('headers', {})}
    try:
        r = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        logger.warning('Booking engine %s %s unreachable: %s', method, path, type(exc).__name__)
        raise BookingEngineUnavailable(type(exc).__name__) from None
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {}
    if 200 <= r.status_code < 300:
        return body
    if r.status_code in (400, 404, 409, 422):
        raise BookingEngineRefused(r.status_code, body if isinstance(body, dict) else {})
    logger.error('Booking engine %s %s answered %s', method, path, r.status_code)
    raise BookingEngineUnavailable(f'HTTP {r.status_code}')


def _all(path: str, params: dict | None = None) -> list[dict]:
    """Every page of a list endpoint."""
    out, page = [], 1
    while True:
        body = _call('GET', path, params={**(params or {}), 'page': page})
        out.extend(body.get('results', []))
        if not body.get('next'):
            return out
        page += 1


# ── Resources and hours ──────────────────────────────────────────────────

def put_resource(ref: str, data: dict) -> dict:
    return _call('PUT', f'/v1/resources/{ref}/', json=data)


def list_resources() -> list[dict]:
    return _all('/v1/resources/')


def put_schedule(ref: str, data: dict) -> dict:
    return _call('PUT', f'/v1/resources/{ref}/schedule/', json=data)


# ── Closures (ONA's calendar blocks) ─────────────────────────────────────

def put_closure(ref: str, data: dict) -> dict:
    return _call('PUT', f'/v1/closures/ref/{ref}/', json=data)


def delete_closure(ref: str) -> None:
    _call('DELETE', f'/v1/closures/ref/{ref}/')


def list_closures() -> list[dict]:
    return _all('/v1/closures/')


# ── Bookings ─────────────────────────────────────────────────────────────

def find_booking(external_ref: str) -> dict | None:
    results = _call('GET', '/v1/bookings/', params={'external_ref': external_ref}).get('results', [])
    return results[0] if results else None


def live_bookings(since_iso: str) -> list[dict]:
    """Held and confirmed bookings ending after `since_iso`."""
    return _all('/v1/bookings/', {'status': 'held,confirmed', 'from': since_iso})


def create_booking(data: dict, idempotency_key: str) -> dict:
    return _call('POST', '/v1/bookings/', json=data, headers={'Idempotency-Key': idempotency_key})


def reschedule_booking(booking_id: str, data: dict) -> dict:
    return _call('POST', f'/v1/bookings/{booking_id}/reschedule/', json=data)


def cancel_booking(booking_id: str, reason: str = '') -> dict:
    return _call('POST', f'/v1/bookings/{booking_id}/cancel/', json={'reason': reason})


def availability(data: dict) -> dict:
    return _call('POST', '/v1/availability/', json=data)
