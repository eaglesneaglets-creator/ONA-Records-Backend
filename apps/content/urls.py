from rest_framework.routers import DefaultRouter

from .views import FaqPublicViewSet

app_name = 'content'

router = DefaultRouter()
router.register('faqs', FaqPublicViewSet, basename='faq')

urlpatterns = router.urls
