from rest_framework.routers import DefaultRouter

from .views import EngineerViewSet, RoomViewSet, ServiceViewSet

app_name = 'studio'

router = DefaultRouter()
router.register('rooms', RoomViewSet, basename='room')
router.register('engineers', EngineerViewSet, basename='engineer')
router.register('services', ServiceViewSet, basename='service')

urlpatterns = router.urls
