from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    BookingViewSet,
    CalendarView,
    EngineerViewSet,
    PackageViewSet,
    RoomViewSet,
    ServiceViewSet,
)

app_name = 'studio'

router = DefaultRouter()
router.register('rooms', RoomViewSet, basename='room')
router.register('engineers', EngineerViewSet, basename='engineer')
router.register('services', ServiceViewSet, basename='service')
router.register('packages', PackageViewSet, basename='package')
router.register('bookings', BookingViewSet, basename='booking')

urlpatterns = [
    path('calendar/', CalendarView.as_view(), name='calendar'),
] + router.urls
