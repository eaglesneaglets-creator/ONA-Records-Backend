from django.urls import path

from .views import CheckoutView, PaystackWebhookView, VerifyView

urlpatterns = [
    path('checkout/', CheckoutView.as_view(), name='payment-checkout'),
    path('verify/', VerifyView.as_view(), name='payment-verify'),
    path('paystack/webhook/', PaystackWebhookView.as_view(), name='paystack-webhook'),
]
