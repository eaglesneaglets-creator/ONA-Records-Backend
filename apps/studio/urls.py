from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import CalendarView, EngineerViewSet, RoomViewSet, ServiceViewSet

app_name = 'studio'

router = DefaultRouter()
router.register('rooms', RoomViewSet, basename='room')
router.register('engineers', EngineerViewSet, basename='engineer')
router.register('services', ServiceViewSet, basename='service')

urlpatterns = [
    path('calendar/', CalendarView.as_view(), name='calendar'),
] + router.urls
