"""ONA Records — root URL configuration."""

from django.conf import settings
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView


def health(request):
    """
    Liveness check for Railway.

    Names the environment deliberately: the most common staging mistake is
    DJANGO_SETTINGS_MODULE not applying, which silently gives you production
    behaviour with staging data. This makes that visible in one request.

    Exempt from SSL redirect (see SECURE_REDIRECT_EXEMPT) so Railway's
    internal check does not follow a 301.
    """
    return JsonResponse({
        'status': 'ok',
        'environment': getattr(settings, 'ENVIRONMENT_NAME', 'production'),
        'debug': settings.DEBUG,
    })


urlpatterns = [
    path('api/v1/health/', health, name='health'),
    path('admin/', admin.site.urls),
    path('api/v1/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/v1/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='docs'),
]
