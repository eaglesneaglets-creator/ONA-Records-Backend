"""
ONA Records — local development settings.

Run with: DJANGO_SETTINGS_MODULE=config.settings.local
"""

from .base import *  # noqa: F401,F403
from decouple import config
import dj_database_url

DEBUG = True

# WhiteNoise is a production static-file server. Keeping it in local/test
# middleware adds no coverage and warns on every request before collectstatic
# has created STATIC_ROOT. Django's development server handles static files.
MIDDLEWARE = [  # noqa: F405
    middleware for middleware in MIDDLEWARE  # noqa: F405
    if middleware != 'whitenoise.middleware.WhiteNoiseMiddleware'
]

SECRET_KEY = config('DJANGO_SECRET_KEY', default='insecure-local-key-do-not-deploy')

ALLOWED_HOSTS = ['localhost', '127.0.0.1', '[::1]', 'testserver']

# Postgres is required even locally. The no-double-booking rule uses an
# EXCLUDE constraint with btree_gist, which SQLite cannot express — so a
# SQLite fallback would let a bug through that production would reject.
DATABASES = {
    'default': dj_database_url.parse(
        # Keep this in sync with docker-compose.yml's host-side port. Port
        # 5432 belongs to any machine-wide Postgres installation; Compose
        # deliberately exposes this project's database on 5433.
        config('DATABASE_URL', default='postgres://ona:ona@localhost:5433/ona_dev'),
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
