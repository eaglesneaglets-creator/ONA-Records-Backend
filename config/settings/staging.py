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

# Railway's health prober sends Host: healthcheck.railway.app, which is not
# the public domain and is not in RAILWAY_PUBLIC_DOMAIN. Without this the
# probe gets a 400 DisallowedHost and the deploy is marked unhealthy even
# though the app is running correctly.
#
# This is safe to allow unconditionally: the prober reaches the container
# over Railway's private network, and the health view returns no data that
# is not already public.
ALLOWED_HOSTS.append('healthcheck.railway.app')

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
MIDDLEWARE = ['core.middleware.staging.NoIndexMiddleware', *MIDDLEWARE]  # noqa: F405

# ---------------------------------------------------------------------------
# 5. Payments — Paystack, test key only
# ---------------------------------------------------------------------------
# Paystack keys say what they are: sk_test_... or sk_live_.... A live key on
# staging would take real money from whoever tests the booking flow, so
# staging refuses to boot with one. No flag to forget; the key itself decides.
if PAYSTACK_SECRET_KEY and not PAYSTACK_SECRET_KEY.startswith('sk_test_'):  # noqa: F405
    raise ImproperlyConfigured(  # noqa: F405
        'PAYSTACK_SECRET_KEY on staging is not a test key (sk_test_...). Refusing to '
        'start: a live key here would charge real cards and Mobile Money wallets.'
    )

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
# Never allow staging to select the production SMTP backend directly. This
# backend still uses SMTP, but replaces every recipient with the controlled
# staging inbox before handing the message off.
EMAIL_BACKEND = 'core.email_backends.RedirectingEmailBackend'
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
        'public': '5000/hour',
        'booking': '200/hour',
        'payment': '200/hour',
        'otp': '50/hour',
    },
}
