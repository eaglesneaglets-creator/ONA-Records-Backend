"""
Refresh-token reuse grace (defect D8).

Refresh tokens rotate: each use returns a new one and blacklists the old.
If the page reloads while a refresh is in flight, the server has already
rotated, but the browser never stored the new token — and the next load sends
the old, now-blacklisted one. The customer is signed out for reloading.

The fix every major identity provider uses (Auth0 and Okta call it a "reuse
interval"): for a few seconds after a rotation, presenting the OLD token again
returns the SAME successor the first call produced. Nothing new is minted, so
a replay gains nothing the legitimate client did not already get, and only
for GRACE_SECONDS.

    remember(old_jti, response)   after a successful rotation
    replay(raw_token)             -> the remembered response, or None
    forget(new_refresh)           on logout: the grace dies with the session

A signature-verified token is required before anything is looked up. The
cache must be shared between workers (Redis in production) for a reload that
lands on another worker; with a per-process cache this degrades to today's
behaviour, never to something weaker.
"""

from __future__ import annotations

from django.core.cache import cache
from rest_framework_simplejwt.exceptions import TokenBackendError
from rest_framework_simplejwt.state import token_backend

GRACE_SECONDS = 30
_KEY = 'jwt-grace:{}'
_BACK = 'jwt-grace-successor:{}'


def _jti(raw: str, *, verify: bool = True) -> str | None:
    try:
        payload = token_backend.decode(raw, verify=verify)
    except TokenBackendError:
        return None
    return payload.get('jti')


def remember(old_jti: str, response: dict) -> None:
    cache.set(_KEY.format(old_jti), response, GRACE_SECONDS)
    new = _jti(response.get('refresh', ''), verify=False)
    if new:
        cache.set(_BACK.format(new), old_jti, GRACE_SECONDS)


def replay(raw: str) -> tuple[dict, dict] | None:
    """(remembered response, verified payload of the old token) — or None."""
    try:
        payload = token_backend.decode(raw, verify=True)   # signature and expiry
    except TokenBackendError:
        return None
    remembered = cache.get(_KEY.format(payload.get('jti')))
    return (remembered, payload) if remembered else None


def forget(refresh: str) -> None:
    new = _jti(refresh, verify=False)
    if not new:
        return
    old = cache.get(_BACK.format(new))
    if old:
        cache.delete(_KEY.format(old))
    cache.delete(_BACK.format(new))
