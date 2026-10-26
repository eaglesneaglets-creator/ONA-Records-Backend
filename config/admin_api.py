"""
/api/v1/admin/ — every staff-only endpoint, in one place.

Each viewset mounted here must carry IsONAAdmin; the tests in
apps/*/tests/test_admin_*.py assert a customer gets 403 on every route this
file registers. Keep staff endpoints here rather than scattered through app
urls, so "what can staff do over the API?" has one answer.
"""

from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.accounts.staff import CustomerStaffViewSet
from apps.marketplace.views import JobAdminViewSet, ProfessionalAdminViewSet
from apps.payments.staff import TransactionStaffViewSet
from apps.studio.staff import StaffBookingViewSet, StaffOverviewView

app_name = 'adminapi'

router = DefaultRouter()
router.register('professionals', ProfessionalAdminViewSet, basename='professional')
router.register('bookings', StaffBookingViewSet, basename='booking')
router.register('customers', CustomerStaffViewSet, basename='customer')
router.register('payments', TransactionStaffViewSet, basename='payment')
router.register('jobs', JobAdminViewSet, basename='job')

urlpatterns = [
    path('overview/', StaffOverviewView.as_view(), name='overview'),
    *router.urls,
]
