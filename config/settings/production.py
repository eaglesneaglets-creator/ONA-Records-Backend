"""
ONA Records — production settings.

Security hardened. staging.py inherits from this file and relaxes only what
must differ, so anything added here reaches staging too.

Run with: DJANGO_SETTINGS_MODULE=config.settings.production
"""

from .base import *  # noqa: F401,F403
from decouple import config, Csv
from django.core.exceptions import ImproperlyConfigured
import dj_database_url

DEBUG = False

SECRET_KEY = config('SECRET_KEY', default='') or config('DJANGO_SECRET_KEY', default='')
if not SECRET_KEY:
    raise ImproperlyConfigured('SECRET_KEY must be set.')

ALLOWED_HOSTS = [h for h in config('ALLOWED_HOSTS', cast=Csv(), default='') if h]

# Railway injects its own domain per environment.
_railway_domain = config('RAILWAY_PUBLIC_DOMAIN', default='')
if _railway_domain:
    ALLOWED_HOSTS.append(_railway_domain)

# Railway's health prober sends Host: healthcheck.railway.app over the private
# network. Without it here the probe 400s and every deploy is marked unhealthy.
ALLOWED_HOSTS.append('healthcheck.railway.app')

# ---------------------------------------------------------------------------
# Database — Railway provides DATABASE_URL; never set it by hand there.
# ---------------------------------------------------------------------------
_database_url = config('DATABASE_URL', default='')
if not _database_url:
    raise ImproperlyConfigured('DATABASE_URL must be set.')

DATABASES = {
    'default': dj_database_url.parse(
        _database_url,
        conn_max_age=config('DB_CONN_MAX_AGE', default=600, cast=int),
        conn_health_checks=True,
        ssl_require=config('DB_SSL_REQUIRE', default=True, cast=bool),
    )
}

# ---------------------------------------------------------------------------
# Cache / Celery
# ---------------------------------------------------------------------------
REDIS_URL = config('REDIS_URL', default='')

if REDIS_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': REDIS_URL,
        }
    }
    SESSION_ENGINE = 'django.contrib.sessions.backends.cache'

CELERY_BROKER_URL = config('CELERY_BROKER_URL', default=REDIS_URL)
CELERY_RESULT_BACKEND = config('CELERY_RESULT_BACKEND', default=REDIS_URL)
CELERY_TASK_ALWAYS_EAGER = False
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# ---------------------------------------------------------------------------
# Transport security
# ---------------------------------------------------------------------------
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_REDIRECT_EXEMPT = [r'^api/v1/health/$']
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'

# Must be the STORAGES dict, not the legacy STATICFILES_STORAGE setting.
# Django only back-fills STORAGES from STATICFILES_STORAGE in narrow cases; here
# it silently fell through to plain StaticFilesStorage, dropping WhiteNoise's
# manifest hashing and precompression. STATICFILES_STORAGE is removed in Django 6.0.
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
EMAIL_BACKEND = config('EMAIL_BACKEND', default='django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = config('EMAIL_HOST', default='')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='ONA Records <no-reply@onarecords.com>')

# ---------------------------------------------------------------------------
# Sentry — optional
# ---------------------------------------------------------------------------
SENTRY_DSN = config('SENTRY_DSN', default='')
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.celery import CeleryIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration(), CeleryIntegration()],
        environment=config('SENTRY_ENVIRONMENT', default='production'),
        traces_sample_rate=config('SENTRY_TRACES_SAMPLE_RATE', default=0.1, cast=float),
        send_default_pii=False,  # this platform holds personal and payment data
    )
