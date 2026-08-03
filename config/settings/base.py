"""
ONA Records — base settings shared by every environment.

Environment-specific modules (local, staging, production, test) import * from
here and override. Nothing here should read a secret with a real default:
if a value must differ per environment, it belongs in that module.
"""

from pathlib import Path
from decouple import config, Csv

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.postgres',
]

THIRD_PARTY_APPS = [
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    'django_filters',
    'drf_spectacular',
]

# One app per bounded context, as set out in the plan.
LOCAL_APPS = [
    'apps.accounts',       # users, roles, profiles
    'apps.studio',         # rooms, engineers, bookings
    'apps.marketplace',    # professional profiles, listings, portfolio
    'apps.requests',       # customer RFQs and proposals
    'apps.projects',       # project lifecycle, milestones, deliverables
    'apps.messaging',      # masked threads, contact-info filtering
    'apps.payments',       # escrow ledger, Hubtel, commission
    'apps.reviews',        # two-way ratings
    'apps.notifications',  # pluggable channels
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'],
    'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.debug',
        'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages',
    ]},
}]

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = 'accounts.User'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 10}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ---------------------------------------------------------------------------
# DRF
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_PAGINATION_CLASS': 'core.pagination.StandardPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_FILTER_BACKENDS': (
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ),
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_THROTTLE_CLASSES': (
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ),
    'DEFAULT_THROTTLE_RATES': {
        'anon': '30/hour',
        'user': '300/hour',
    },
    'EXCEPTION_HANDLER': 'core.exceptions.handler.api_exception_handler',
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'ONA Records API',
    'DESCRIPTION': 'Studio booking and creative marketplace.',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
}

from datetime import timedelta  # noqa: E402

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=30),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

# ---------------------------------------------------------------------------
# Locale — Ghana
# ---------------------------------------------------------------------------
LANGUAGE_CODE = 'en-gb'
TIME_ZONE = 'Africa/Accra'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
# Amounts are stored as INTEGER PESEWAS everywhere. GHS 12.50 is 1250.
# Never float, never Decimal in the database: floats lose cents, and mixing
# Decimal with integer arithmetic is how rounding bugs reach a ledger.
CURRENCY = 'GHS'
CURRENCY_MINOR_UNITS = 100

# Default for NEW transaction rows only. The rate that applied is stored on
# each row, so changing this must never alter what a past payment was worth.
DEFAULT_COMMISSION_PERCENT = config('DEFAULT_COMMISSION_PERCENT', default=15, cast=int)

# Flat studio booking fee, in pesewas.
BOOKING_FEE_PESEWAS = config('BOOKING_FEE_PESEWAS', default=4800, cast=int)

FREE_CANCELLATION_HOURS = config('FREE_CANCELLATION_HOURS', default=48, cast=int)

# ---------------------------------------------------------------------------
# Hubtel — mobile money
# ---------------------------------------------------------------------------
HUBTEL_API_KEY = config('HUBTEL_API_KEY', default='')
HUBTEL_API_SECRET = config('HUBTEL_API_SECRET', default='')
HUBTEL_MERCHANT_ID = config('HUBTEL_MERCHANT_ID', default='')
HUBTEL_POS_SALES_ID = config('HUBTEL_POS_SALES_ID', default='')
HUBTEL_CALLBACK_URL = config('HUBTEL_CALLBACK_URL', default='')

HUBTEL_SMS_CLIENT_ID = config('HUBTEL_SMS_CLIENT_ID', default='')
HUBTEL_SMS_CLIENT_SECRET = config('HUBTEL_SMS_CLIENT_SECRET', default='')
HUBTEL_SMS_SENDER_ID = config('HUBTEL_SMS_SENDER_ID', default='ONARecords')

# ---------------------------------------------------------------------------
# Cloudinary
# ---------------------------------------------------------------------------
CLOUDINARY_CLOUD_NAME = config('CLOUDINARY_CLOUD_NAME', default='')
CLOUDINARY_API_KEY = config('CLOUDINARY_API_KEY', default='')
CLOUDINARY_API_SECRET = config('CLOUDINARY_API_SECRET', default='')
CLOUDINARY_FOLDER_PREFIX = 'ona'

# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------
FRONTEND_URL = config('FRONTEND_URL', default='http://localhost:3000')
CORS_ALLOWED_ORIGINS = [o for o in config('CORS_ALLOWED_ORIGINS', cast=Csv(), default='') if o]
CSRF_TRUSTED_ORIGINS = [o for o in config('CSRF_TRUSTED_ORIGINS', cast=Csv(), default='') if o]

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {'format': '{levelname} {asctime} {name} {message}', 'style': '{'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'verbose'},
    },
    'root': {'handlers': ['console'], 'level': config('LOG_LEVEL', default='INFO')},
    'loggers': {
        'django.request': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False},
    },
}
