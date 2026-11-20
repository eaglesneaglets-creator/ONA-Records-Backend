"""
/api/v1/kyc/ — a signed-in customer (or professional) verifying their identity.

    GET    /kyc/            where my check stands (and whether checks are available)
    POST   /kyc/            start one, or resume the open one
    PATCH  /kyc/details/    name, date of birth, ID type and number
    POST   /kyc/documents/  one photo: id_front, id_back or selfie
    POST   /kyc/submit/     send for review
    POST   /kyc/webhook/    the KYC service telling ONA something changed

Every answer carries the check as the KYC service describes it (status, what
is still missing, the last four characters of the ID number — never the
whole number, never the images).
"""

import json
import logging
import uuid

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import client as kyc
from .models import IdentityStatus, current_check, remember

logger = logging.getLogger(__name__)


class CanVerify(BasePermission):
    message = 'Identity checks are for customer and professional accounts.'

    def has_permission(self, request, view):
        return getattr(request.user, 'role', None) in ('customer', 'professional')


class _Base(APIView):
    permission_classes = [IsAuthenticated, CanVerify]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'kyc'

    def open_check(self, request):
        check = current_check(request.user)
        if check is None:
            raise NotFound('Start your identity check first.')
        return check

    def answer(self, request, v: dict, code=status.HTTP_200_OK):
        remember(request.user, v)
        return Response({'available': True, 'verification': v}, status=code)


class MyIdentityView(_Base):
    def get(self, request):
        if not kyc.is_configured():
            return Response({'available': False, 'verification': None})
        check = current_check(request.user)
        if check is None:
            return Response({'available': True, 'verification': None})
        return self.answer(request, kyc.get(check.verification_id))

    def post(self, request):
        check = current_check(request.user)
        if check and check.status == IdentityStatus.APPROVED:
            raise kyc.KycConflict('Your identity is already verified.')
        return self.answer(request, kyc.start(request.user.pk), status.HTTP_201_CREATED)


class DetailsView(_Base):
    FIELDS = ('full_name', 'date_of_birth', 'id_type', 'id_number')

    def patch(self, request):
        check = self.open_check(request)
        data = {k: request.data[k] for k in self.FIELDS if k in request.data}
        if not data:
            raise ValidationError({'detail': 'Nothing to change.'})
        return self.answer(request, kyc.update(check.verification_id, data))


class DocumentView(_Base):
    def post(self, request):
        check = self.open_check(request)
        kind = request.data.get('kind')
        file = request.FILES.get('file')
        if kind not in kyc.DOCUMENT_KINDS:
            raise ValidationError({'kind': 'Choose id_front, id_back or selfie.'})
        if file is None:
            raise ValidationError({'file': 'Choose a photo.'})
        if file.size > kyc.MAX_UPLOAD_BYTES:
            raise ValidationError({'file': 'That photo is larger than 10 MB.'})
        kyc.upload(check.verification_id, kind, file)
        # Answer with the whole check, so the page knows what is still missing.
        return self.answer(request, kyc.get(check.verification_id), status.HTTP_201_CREATED)


class SubmitView(_Base):
    def post(self, request):
        check = self.open_check(request)
        return self.answer(request, kyc.submit(check.verification_id))


class KycWebhookView(APIView):
    """
    The KYC service says a check changed. The message is only a nudge: ONA
    asks KYC for the check's current state and stores that, so a forged,
    replayed or out-of-order message cannot set a status by itself.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    def post(self, request):
        raw = request.body
        if not kyc.signature_is_valid(raw, request.headers.get('KYC-Signature')):
            return Response(status=status.HTTP_401_UNAUTHORIZED)
        try:
            data = (json.loads(raw).get('data')) or {}
            user_id = uuid.UUID(str(data.get('subject_ref')))
            verification_id = uuid.UUID(str(data.get('verification_id')))
        except (ValueError, AttributeError, TypeError):
            return Response(status=status.HTTP_200_OK)  # well-signed but not about an ONA user

        user = get_user_model().objects.filter(pk=user_id).first()
        if user is None:
            return Response(status=status.HTTP_200_OK)
        try:
            v = kyc.get(verification_id)
        except kyc.KycUnavailable:
            # Non-2xx: the KYC service retries later, which is what we want.
            return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except NotFound:
            return Response(status=status.HTTP_200_OK)
        if v.get('subject_ref') == str(user.pk):
            remember(user, v)
        return Response(status=status.HTTP_200_OK)
