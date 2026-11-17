from django.urls import path

from .views import DetailsView, DocumentView, KycWebhookView, MyIdentityView, SubmitView

urlpatterns = [
    path('', MyIdentityView.as_view(), name='kyc-mine'),
    path('details/', DetailsView.as_view(), name='kyc-details'),
    path('documents/', DocumentView.as_view(), name='kyc-documents'),
    path('submit/', SubmitView.as_view(), name='kyc-submit'),
    path('webhook/', KycWebhookView.as_view(), name='kyc-webhook'),
]
