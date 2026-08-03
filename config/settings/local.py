"""
ONA Records — local development settings.

Run with: DJANGO_SETTINGS_MODULE=config.settings.local
"""

from .base import *  # noqa: F401,F403
from decouple import config
import dj_database_url

DEBUG = True

SECRET_KEY = config('DJANGO_SECRET_KEY', default='insecure-local-key-do-not-deploy')

ALLOWED_HOSTS = ['localhost', '127.0.0.1', '[::1]', 'testserver']

# Postgres is required even locally. The no-double-booking rule uses an
# EXCLUDE constraint with btree_gist, which SQLite cannot express — so a
# SQLite fallback would let a bug through that production would reject.
DATABASES = {
    'default': dj_database_url.parse(
        config('DATABASE_URL', default='postgres://ona:ona@localhost:5432/ona_dev'),
        conn_max_age=0,
    )
}

CORS_ALLOW_ALL_ORIGINS = True

EMAIL_BACKEND = config(
    'EMAIL_BACKEND',
    default='django.core.mail.backends.console.EmailBackend',
)
DEFAULT_FROM_EMAIL = 'ONA Records <dev@localhost>'

# Run tasks inline — no Redis or worker needed to develop.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

LOGGING['root']['level'] = 'DEBUG'  # noqa: F405
