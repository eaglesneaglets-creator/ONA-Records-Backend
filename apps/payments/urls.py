from django.urls import path
from rest_framework.routers import SimpleRouter

from .mine import MyPaymentViewSet
from .views import CheckoutView, PaystackWebhookView, VerifyView

router = SimpleRouter()
router.register('mine', MyPaymentViewSet, basename='my-payment')

urlpatterns = [
    path('checkout/', CheckoutView.as_view(), name='payment-checkout'),
    path('verify/', VerifyView.as_view(), name='payment-verify'),
    path('paystack/webhook/', PaystackWebhookView.as_view(), name='paystack-webhook'),
    *router.urls,
]
