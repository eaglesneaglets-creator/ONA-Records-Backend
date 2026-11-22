from django.urls import path
from rest_framework.routers import DefaultRouter

from core.business_api import BusinessRulesPublicView

from .views import ContactPublicView, FaqPublicViewSet, MediaPublicView, SitePublicView

app_name = 'content'

router = DefaultRouter()
router.register('faqs', FaqPublicViewSet, basename='faq')

urlpatterns = [
    path('contact/', ContactPublicView.as_view(), name='contact'),
    path('rules/', BusinessRulesPublicView.as_view(), name='rules'),
    path('site/', SitePublicView.as_view(), name='site'),
    path('media/<uuid:pk>/', MediaPublicView.as_view(), name='media'),
    *router.urls,
]
