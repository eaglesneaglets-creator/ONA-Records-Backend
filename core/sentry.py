"""
Error reporting to Sentry, without the personal data this platform holds.

production.py calls init_sentry() when SENTRY_DSN is set. send_default_pii
alone is not enough: with it off, sentry-sdk 2.x still ships every stack
frame's local variables, the parsed request body (up to ~10 KB) and the raw
query string, and its key scrubber is exact-match and top-level only. Here
that would mean, among others:

  - the KYC bearer key (a local in kyc/client.py) and Paystack webhook
    payloads (customer email, phone, card bin/last4) as frame variables;
  - passwords, refresh tokens, id_number and date_of_birth in request bodies;
  - verify/reset links (uid + token) inside Email objects being sent;
  - an email address inside an IntegrityError message.

So the options below switch those channels off at the source, and
before_send strips what is left: query strings, request bodies, the user,
and any email address, Ghana phone number or Ghana Card number that ended up
in a message. Sentry's own server-side scrubbers stay on as a second line.

Deliberately NOT the newer `data_collection` option: it defaults user_info to
on and makes the SDK ignore a custom event_scrubber.
"""

from __future__ import annotations

import os
import re
from urllib.parse import unquote

_EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
# Ghana numbers, local (024 123 4567) or international (+233 24 123 4567).
_PHONE = re.compile(r'(?<!\d)(?:\+?233|0)[\s-]?\d{2}[\s-]?\d{3}[\s-]?\d{4}(?!\d)')
_GHANA_CARD = re.compile(r'\bGHA-?\d{9}-?\d\b', re.IGNORECASE)

# Keys whose VALUES are scrubbed wherever they appear (recursively), on top of
# Sentry's own denylist (password, token, secret, authorization, cookie, ...).
EXTRA_DENYLIST = [
    'refresh', 'access', 'uid', 'password_confirm', 'current_password', 'new_password',
    'email', 'phone', 'first_name', 'last_name', 'full_name', 'date_of_birth',
    'id_type', 'id_number', 'recipient', 'reviewer', 'customer',
    'authorization_code', 'access_code', 'x-paystack-signature', 'kyc-signature',
    # Outgoing-call query strings in breadcrumbs and spans: the KYC review
    # calls send ?reviewer=<staff email>.
    'http.query', 'http.fragment',
]

# The only request headers an event keeps. Everything else (signatures,
# forwarded IPs, Referer with its query) is dropped rather than denylisted.
_KEEP_HEADERS = {'user-agent', 'content-type', 'content-length', 'accept', 'host'}


def mask(text):
    """Replace email addresses, phone numbers and Ghana Card numbers in a string."""
    if not isinstance(text, str):
        return text
    if '%' in text:
        text = unquote(text)   # a@b.com arrives as a%40b.com inside URLs
    text = _EMAIL.sub('[email]', text)
    text = _GHANA_CARD.sub('[id]', text)
    return _PHONE.sub('[phone]', text)


def _strip_url(url):
    if not isinstance(url, str):
        return url
    return re.split(r'[?#]', url, maxsplit=1)[0]


def _scrub_breadcrumb_dict(crumb: dict) -> dict:
    crumb['message'] = mask(crumb.get('message'))
    data = crumb.get('data')
    if isinstance(data, dict):
        data.pop('http.query', None)
        data.pop('http.fragment', None)
        for key in ('url', 'from', 'to'):
            if key in data:
                data[key] = _strip_url(data[key])
    return crumb


def before_send(event, hint):
    """Last pass over every error and transaction before it leaves the server."""
    request = event.get('request')
    if isinstance(request, dict):
        for key in ('data', 'cookies', 'query_string', 'env'):
            request.pop(key, None)
        request['url'] = _strip_url(request.get('url'))
        headers = request.get('headers')
        if isinstance(headers, dict):
            for name in list(headers):
                if name.lower() not in _KEEP_HEADERS:
                    headers.pop(name)

    event.pop('user', None)
    event['message'] = mask(event.get('message')) if event.get('message') else event.get('message')

    logentry = event.get('logentry')
    if isinstance(logentry, dict):
        logentry['message'] = mask(logentry.get('message'))
        logentry['formatted'] = mask(logentry.get('formatted'))
        if logentry.get('params'):
            logentry['params'] = ['[Filtered]' for _ in logentry['params']]

    for exc in (event.get('exception') or {}).get('values') or []:
        exc['value'] = mask(exc.get('value'))

    crumbs = event.get('breadcrumbs')
    values = crumbs.get('values') if isinstance(crumbs, dict) else crumbs
    for crumb in values or []:
        if isinstance(crumb, dict):
            _scrub_breadcrumb_dict(crumb)

    if isinstance(event.get('transaction'), str):
        event['transaction'] = _strip_url(event['transaction'])
    _scrub_spans(event)
    return event


def _scrub_spans(event) -> None:
    """Transactions (traces_sample_rate) carry spans: outgoing calls with their
    query, SQL, and Django template renders with the whole template context."""
    for span in event.get('spans') or []:
        if not isinstance(span, dict):
            continue
        op = str(span.get('op') or '')
        if op.startswith(('cache.', 'db.redis')):
            span['description'] = op   # the description is the key, maybe a session key
        else:
            span['description'] = mask(_strip_url(span.get('description')))
        data = span.get('data')
        if isinstance(data, dict):
            for key in ('http.query', 'http.fragment', 'context', 'redis.key', 'cache.key'):
                data.pop(key, None)
            if 'url' in data:
                data['url'] = _strip_url(data['url'])
        tags = span.get('tags')
        if isinstance(tags, dict):
            tags.pop('redis.key', None)


def before_breadcrumb(crumb, hint):
    # Redis keys include Django session keys (sessions live in the cache).
    if crumb.get('category') == 'redis':
        return None
    return _scrub_breadcrumb_dict(crumb)


def default_environment() -> str:
    """staging.py star-imports production.py, so the settings module is the
    only thing that tells them apart while production.py is still running."""
    module = os.environ.get('DJANGO_SETTINGS_MODULE', '')
    return 'staging' if module.endswith('.staging') else 'production'


def default_release() -> str | None:
    """The deployed commit. Railway sets RAILWAY_GIT_COMMIT_SHA at runtime;
    sentry-sdk does not look for it, and the image has no .git to ask."""
    return os.environ.get('SENTRY_RELEASE') or os.environ.get('RAILWAY_GIT_COMMIT_SHA') or None


def init_sentry(dsn: str, *, environment: str, release: str | None, traces_sample_rate: float) -> None:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import ignore_logger
    from sentry_sdk.integrations.redis import RedisIntegration
    from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

    sentry_sdk.init(
        dsn=dsn,
        integrations=[DjangoIntegration(), CeleryIntegration()],
        # Its breadcrumbs and spans name cache keys, and sessions are cache
        # keys: a staff session id would be a live credential in Sentry.
        disabled_integrations=[RedisIntegration()],
        environment=environment,
        release=release,
        traces_sample_rate=traces_sample_rate,
        send_default_pii=False,          # no user, IP, cookies or headers with credentials
        include_local_variables=False,   # frame variables hold keys, payloads, Email objects
        max_request_body_size='never',   # bodies hold passwords, tokens, KYC fields
        trace_propagation_targets=[],    # no trace headers to Paystack, KYC or S3
        event_scrubber=EventScrubber(recursive=True,
                                     denylist=[*DEFAULT_DENYLIST, *EXTRA_DENYLIST]),
        before_send=before_send,
        before_send_transaction=before_send,
        before_breadcrumb=before_breadcrumb,
    )
    # Scanners hitting the API with a bad Host header: noise, not errors.
    ignore_logger('django.security.DisallowedHost')
