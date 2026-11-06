"""ONA Records — root URL configuration."""

from django.conf import settings
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from core.admin_security import LockoutAdminAuthenticationForm

# The Django admin shares the API's per-account sign-in lockout (Task 18).
admin.site.login_form = LockoutAdminAuthenticationForm


def health(request):
    """
    Liveness check for Railway.

    Names the environment deliberately: the most common staging mistake is
    DJANGO_SETTINGS_MODULE not applying, which silently gives you production
    behaviour with staging data. This makes that visible in one request.

    Exempt from SSL redirect (see SECURE_REDIRECT_EXEMPT) so Railway's
    internal check does not follow a 301.
    """
    body = {
        'status': 'ok',
        'environment': getattr(settings, 'ENVIRONMENT_NAME', 'production'),
        'debug': settings.DEBUG,
    }
    # ?deep=1 — for an uptime monitor, not Railway's liveness check: also
    # touches the database and the cache, and answers 503 if either is down.
    # Liveness stays shallow on purpose, so a database blip does not make
    # Railway restart a healthy web process.
    if request.GET.get('deep'):
        from django.core.cache import cache
        from django.db import connection
        checks = {}
        try:
            with connection.cursor() as cur:
                cur.execute('SELECT 1')
            checks['database'] = 'ok'
        except Exception:  # noqa: BLE001 — the detail is in the logs, not public
            checks['database'] = 'down'
        try:
            cache.set('health:deep', 1, 10)
            checks['cache'] = 'ok' if cache.get('health:deep') == 1 else 'down'
        except Exception:  # noqa: BLE001
            checks['cache'] = 'down'
        body['checks'] = checks
        if 'down' in checks.values():
            body['status'] = 'degraded'
            return JsonResponse(body, status=503)
    return JsonResponse(body)


urlpatterns = [
    path('api/v1/health/', health, name='health'),
    path('api/v1/auth/', include('apps.accounts.urls')),
    path('api/v1/studio/', include('apps.studio.urls')),
    path('api/v1/content/', include('apps.content.urls')),
    path('api/v1/payments/', include('apps.payments.urls')),
    path('api/v1/admin/', include('config.admin_api')),
    path(settings.ADMIN_URL, admin.site.urls),  # login form: core/admin_security.py
]

# The API map is useful in development and staging and a free reconnaissance
# document in production.
if getattr(settings, 'ENVIRONMENT_NAME', 'production') != 'production':
    urlpatterns += [
        path('api/v1/schema/', SpectacularAPIView.as_view(), name='schema'),
        path('api/v1/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='docs'),
    ]
