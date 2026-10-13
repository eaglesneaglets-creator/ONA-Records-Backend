"""
/api/v1/admin/ — every staff-only endpoint, in one place.

Each viewset mounted here must carry IsONAAdmin; the tests in
apps/*/tests/test_admin_*.py assert a customer gets 403 on every route this
file registers. Keep staff endpoints here rather than scattered through app
urls, so "what can staff do over the API?" has one answer.
"""

from rest_framework.routers import DefaultRouter

from apps.marketplace.views import ProfessionalAdminViewSet

app_name = 'adminapi'

router = DefaultRouter()
router.register('professionals', ProfessionalAdminViewSet, basename='professional')

urlpatterns = router.urls
