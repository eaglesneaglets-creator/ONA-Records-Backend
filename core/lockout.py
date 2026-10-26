"""
Per-account sign-in lockout (Task 18, launch blocker "admin login lockout").

The anon throttle limits one IP. This limits one ACCOUNT: after
LOCKOUT_ATTEMPTS failed sign-ins for the same email within LOCKOUT_WINDOW,
further attempts for that email are refused until the window passes — so a
password cannot be guessed by rotating IP addresses.

  - Keyed on a hash of the lower-cased email, whether or not an account
    exists: the response never reveals which addresses are registered.
  - A correct password resets the count.
  - The moment an account locks is written to AuditLog (no password, no
    attempt contents) so staff can see an attack on a named account.
  - Shared by the API login and the Django admin login.

Counts live in the cache — Redis in production (config/settings/production.py),
so all web workers share them.
"""

from __future__ import annotations

import hashlib

from django.conf import settings
from django.core.cache import cache

LOCKOUT_ATTEMPTS = getattr(settings, 'LOCKOUT_ATTEMPTS', 5)
LOCKOUT_WINDOW = getattr(settings, 'LOCKOUT_WINDOW_SECONDS', 15 * 60)

LOCKED_MESSAGE = ('Too many failed sign-ins for this account. Wait 15 minutes and try again, '
                  'or reset your password.')


def _key(email: str) -> str:
    digest = hashlib.sha256((email or '').strip().lower().encode()).hexdigest()
    return f'signin-fail:{digest}'


def is_locked(email: str) -> bool:
    return (cache.get(_key(email)) or 0) >= LOCKOUT_ATTEMPTS


def record_failure(email: str, request=None) -> bool:
    """Count a failed attempt. Returns True if this attempt locked the account."""
    key = _key(email)
    cache.add(key, 0, LOCKOUT_WINDOW)
    try:
        count = cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, LOCKOUT_WINDOW)
        count = 1
    if count == LOCKOUT_ATTEMPTS:
        from core.models import AuditLog
        AuditLog.record('auth.locked_out', request=request,
                        email=(email or '').strip().lower()[:254],
                        attempts=count, window_seconds=LOCKOUT_WINDOW)
        return True
    return False


def clear(email: str) -> None:
    cache.delete(_key(email))
