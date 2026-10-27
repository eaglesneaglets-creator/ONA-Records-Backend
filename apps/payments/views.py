"""
Payment endpoints (Tasks 8 and 9, ADR-018).

    POST /api/v1/payments/checkout/          {booking}    -> {authorization_url, reference}
    POST /api/v1/payments/verify/            {reference}  -> outcome, for the return page
    POST /api/v1/payments/paystack/webhook/               Paystack only; signed

The webhook is the one endpoint in the API with no session: Paystack cannot
log in. Its authentication is the HMAC signature, checked over the raw body
before anything is parsed. A request without a valid signature is a 401 and
touches nothing.
"""

from __future__ import annotations

import json
import logging

from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.studio.models import StudioBooking

from . import paystack
from .checkout import CheckoutRefused, settle, start_checkout
from .ledger import LedgerError
from .models import Provider, Transaction

logger = logging.getLogger(__name__)


class _PaymentThrottled(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'payment'


class CheckoutSerializer(serializers.Serializer):
    booking = serializers.UUIDField()


class CheckoutView(_PaymentThrottled):
    def post(self, request):
        body = CheckoutSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        # Your own booking only; someone else's id is a 404, not a 403, so
        # ids cannot be probed.
        booking = StudioBooking.objects.filter(pk=body.validated_data['booking'],
                                               customer=request.user).first()
        if booking is None:
            return Response({'detail': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)
        try:
            url, reference = start_checkout(booking, request.user, request=request)
        except (CheckoutRefused, LedgerError) as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except paystack.PaystackError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({'authorization_url': url, 'reference': reference},
                        status=status.HTTP_201_CREATED)


class VerifySerializer(serializers.Serializer):
    reference = serializers.CharField(max_length=100)


class VerifyView(_PaymentThrottled):
    """The return page's question: "did my payment go through?" """

    def post(self, request):
        body = VerifySerializer(data=request.data)
        body.is_valid(raise_exception=True)
        ref = body.validated_data['reference']
        txn = (Transaction.objects.filter(provider=Provider.PAYSTACK, provider_ref=ref,
                                          customer=request.user)
               .select_related('booking').first())
        if txn is None:
            return Response({'detail': 'Payment not found.'}, status=status.HTTP_404_NOT_FOUND)
        checked = True
        try:
            txn = settle(ref) or txn
        except (paystack.PaystackError, LedgerError) as exc:
            # Paystack slow or down: say "still checking" rather than "failed".
            # The webhook and the sweep will finish the job.
            logger.warning('Return-page verify deferred: ref=%s %s', ref, exc)
            checked = False
        booking = StudioBooking.objects.get(pk=txn.booking_id)
        return Response({
            'status': txn.status,
            'checked': checked,
            'amount_display': txn.amount_display,
            'failure_reason': txn.failure_reason,
            'booking': {'id': str(booking.pk), 'reference': booking.reference,
                        'status': booking.status},
        })


class PaystackWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    # Paystack retries anything that is not a 200 for up to 72 hours. A
    # throttle here would turn a burst of legitimate events into retries.
    throttle_classes = []

    def post(self, request):
        raw = request.body  # before request.data: the signature is over these bytes
        if not paystack.signature_is_valid(raw, request.headers.get('x-paystack-signature')):
            return Response(status=status.HTTP_401_UNAUTHORIZED)
        try:
            event = json.loads(raw)
        except ValueError:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        if event.get('event') != 'charge.success':
            return Response(status=status.HTTP_200_OK)  # acknowledged; not ours to act on
        reference = str((event.get('data') or {}).get('reference') or '')
        if not reference:
            return Response(status=status.HTTP_200_OK)
        try:
            settle(reference)
        except paystack.PaystackError:
            # Could not verify right now: a non-200 makes Paystack retry later,
            # which is exactly what we want.
            return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except LedgerError as exc:
            # A retry would hit the same wall. It is in the logs and the audit
            # trail for a person.
            logger.error('Paystack webhook ledger error: ref=%s %s', reference, exc)
        return Response(status=status.HTTP_200_OK)
