"""
ONA Records — staging settings.

Staging exists to catch what local cannot: real Postgres constraints, real
webhook round-trips, real cold starts. So it inherits production almost
entirely. Anything that differs is listed here with a reason — if a setting
is not in this file, staging behaves exactly like production.

Run with: DJANGO_SETTINGS_MODULE=config.settings.staging

WHY NOT JUST USE PRODUCTION
    This platform holds other people's money. A webhook bug in production
    releases escrow that cannot be recovered, and a live Paystack key means a
    test charge is a real charge. Staging is the place those mistakes are
    survivable.
"""

from .production import *  # noqa: F401,F403
from decouple import config, Csv

# ---------------------------------------------------------------------------
# 1. Identity — so nobody mistakes this for the real thing
# ---------------------------------------------------------------------------
ENVIRONMENT_NAME = 'staging'

# Surfaced by the health endpoint and included in every Sentry event, so an
# alert says which environment it came from.
SENTRY_ENVIRONMENT = 'staging'

# ---------------------------------------------------------------------------
# 2. Debug stays OFF
# ---------------------------------------------------------------------------
# Tempting to turn on, but DEBUG=True changes query behaviour, disables
# security middleware and leaks stack traces. Staging that behaves
# differently from production is not staging. Use logging instead.
DEBUG = False

LOGGING['root']['level'] = config('LOG_LEVEL', default='INFO')  # noqa: F405
LOGGING['loggers']['django.request']['level'] = 'DEBUG'  # noqa: F405

# ---------------------------------------------------------------------------
# 3. Hosts and origins
# ---------------------------------------------------------------------------
ALLOWED_HOSTS = [h for h in config('ALLOWED_HOSTS', cast=Csv(), default='') if h]

# Railway injects its own public domain per environment.
_railway_domain = config('RAILWAY_PUBLIC_DOMAIN', default='')
if _railway_domain:
    ALLOWED_HOSTS.append(_railway_domain)

CORS_ALLOWED_ORIGINS = [o for o in config('CORS_ALLOWED_ORIGINS', cast=Csv(), default='') if o]
CSRF_TRUSTED_ORIGINS = [o for o in config('CSRF_TRUSTED_ORIGINS', cast=Csv(), default='') if o]

# ---------------------------------------------------------------------------
# 4. Search engines must not index staging
# ---------------------------------------------------------------------------
# A staging copy of a marketplace competing with production in search results
# is a real and avoidable problem. The middleware sets X-Robots-Tag on every
# response; robots.txt alone is not enough because it does not stop indexing
# of pages already discovered.
STAGING_NOINDEX = True

# ---------------------------------------------------------------------------
# 5. Payments — Hubtel, test credentials only
# ---------------------------------------------------------------------------
# Hubtel does not prefix its credentials the way some providers do, so there
# is no reliable way to detect a live key by inspecting it. The guard is
# therefore an explicit declaration: staging must state that it knows it is
# using test credentials. Set HUBTEL_TEST_MODE=True in the staging
# environment. Forgetting to set it stops the deploy rather than silently
# charging real phones.
HUBTEL_TEST_MODE = config('HUBTEL_TEST_MODE', default=False, cast=bool)

if HUBTEL_API_KEY and not HUBTEL_TEST_MODE:  # noqa: F405
    raise ImproperlyConfigured(  # noqa: F405
        'Hubtel credentials are set on staging but HUBTEL_TEST_MODE is not '
        'True. Refusing to start: a live Hubtel account here would send real '
        'mobile money prompts to real phones during testing. Set '
        'HUBTEL_TEST_MODE=True once you have confirmed the credentials are '
        'for a test/sandbox merchant account.'
    )

# Hubtel's callback must be publicly reachable. Railway gives each
# environment its own domain, so staging and production never share one.
HUBTEL_CALLBACK_URL = config('HUBTEL_CALLBACK_URL', default='')

# ---------------------------------------------------------------------------
# 6. Media — separate folder tree
# ---------------------------------------------------------------------------
# Same Cloudinary account is fine; the same folders are not. Test uploads
# must not appear in the real media library, and clearing staging media must
# never touch production. core/storage.py routes by this prefix.
CLOUDINARY_FOLDER_PREFIX = 'ona-staging'

# ---------------------------------------------------------------------------
# 7. Email — nothing reaches a real person
# ---------------------------------------------------------------------------
# Staging has copies of real bookings, which means real addresses. Sending
# from staging means emailing customers about bookings that did not happen.
# Everything is redirected to one inbox instead.
EMAIL_BACKEND = config(
    'EMAIL_BACKEND',
    default='django.core.mail.backends.console.EmailBackend',
)
STAGING_EMAIL_REDIRECT_TO = config('STAGING_EMAIL_REDIRECT_TO', default='')

DEFAULT_FROM_EMAIL = config(
    'DEFAULT_FROM_EMAIL',
    default='ONA Records STAGING <no-reply-staging@onarecords.example>',
)
EMAIL_SUBJECT_PREFIX = '[STAGING] '

# ---------------------------------------------------------------------------
# 8. Rate limits — relaxed, because testing is bursty
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {  # noqa: F405
    **REST_FRAMEWORK,  # noqa: F405
    'DEFAULT_THROTTLE_RATES': {
        'anon': '200/hour',
        'user': '2000/hour',
        'otp': '50/hour',
    },
}
