"""
/api/v1/admin/kyc/ — ONA staff reviewing ONA users' identity checks.

The KYC service holds the data and the rules; this relays them to the staff
console and adds what only ONA knows: which account the check belongs to.
Every decision is made in the KYC service (its transition table, its row
locks, its audit trail, which records "platform:ona-records/<staff email>")
and also written to ONA's own audit log.

Scans are relayed as bytes, never as bucket links, so the browser shows
them from blob: URLs and no link to an ID card can leak from a page.
"""

import uuid

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import AuditLog
from core.permissions.roles import IsONAAdmin

from . import client as kyc
from .models import IdentityCheck, remember

FILTERS = {
    'waiting': 'submitted,in_review',
    'changes_requested': 'changes_requested',
    'approved': 'approved',
    'rejected': 'rejected',
}
DECISIONS = ('approved', 'rejected', 'changes_requested')
# The KYC service pages its review queue in fifties.
KYC_PAGE_SIZE = 50


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _person(user, *, full=False) -> dict | None:
    if user is None:
        return None
    out = {'id': str(user.pk), 'public_ref': user.public_ref, 'name': user.full_name or user.email,
           'email': user.email, 'role': user.role}
    if full:
        out.update({'phone': user.phone, 'date_joined': user.date_joined.isoformat()})
    return out


def _users(refs) -> dict:
    ids = [u for u in (_uuid(r) for r in refs) if u]
    return {str(u.pk): u for u in get_user_model().objects.filter(pk__in=ids)}


class KycReviewViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated, IsONAAdmin]
    lookup_value_regex = '[0-9a-f-]{36}'

    def _reviewer(self, request) -> str:
        return request.user.email

    def list(self, request):
        which = request.query_params.get('filter', 'waiting')
        if which not in FILTERS:
            raise ValidationError({'filter': f'Choose one of: {", ".join(FILTERS)}.'})
        try:
            page = max(1, int(request.query_params.get('page', 1)))
        except ValueError:
            page = 1
        data = kyc.queue(FILTERS[which], page)
        users = _users(r['subject_ref'] for r in data.get('results', []))
        rows = [{**r, 'person': _person(users.get(r['subject_ref']))} for r in data.get('results', [])]
        return Response({'count': data.get('count', len(rows)), 'page_size': KYC_PAGE_SIZE, 'results': rows})

    def retrieve(self, request, pk=None):
        v = kyc.review_detail(pk, self._reviewer(request))
        user = _users([v.get('subject_ref')]).get(v.get('subject_ref'))
        if user is not None:
            remember(user, v)
        # Bucket links stay on the server; the console fetches scans through
        # the documents action below.
        docs = [{k: val for k, val in d.items() if k != 'url'} for d in v.get('documents', [])]
        return Response({**v, 'documents': docs, 'person': _person(user, full=True)})

    # Any value matches the URL, so the staff check answers first for
    # everyone else (core/tests/test_rbac_sweep.py); the kind is checked here.
    @action(detail=True, methods=['get'], url_path=r'documents/(?P<kind>[^/.]+)')
    def document(self, request, pk=None, kind=None):
        if kind not in kyc.DOCUMENT_KINDS:
            raise NotFound('No such document.')
        body, content_type = kyc.document(pk, kind, self._reviewer(request))
        resp = HttpResponse(body, content_type=content_type)
        resp['Cache-Control'] = 'no-store, private'
        resp['X-Content-Type-Options'] = 'nosniff'
        return resp

    @action(detail=True, methods=['post'], url_path='start-review')
    def start_review(self, request, pk=None):
        v = kyc.start_review(pk, self._reviewer(request))
        IdentityCheck.objects.filter(pk=pk).update(status=v['status'])
        return Response(v)

    @action(detail=True, methods=['post'])
    def decision(self, request, pk=None):
        decision = request.data.get('decision')
        reason = (request.data.get('reason') or '').strip()
        if decision not in DECISIONS:
            raise ValidationError({'decision': 'Choose approved, rejected or changes_requested.'})
        v = kyc.decide(pk, decision, reason, self._reviewer(request))
        IdentityCheck.objects.filter(pk=pk).update(status=v['status'], reason=reason)
        user = _users([v.get('subject_ref')]).get(v.get('subject_ref'))
        AuditLog.record(AuditLog.Action.IDENTITY_DECIDED, actor=request.user, target=user, request=request,
                        verification_id=str(pk), decision=decision, reason=reason)
        return Response(v)
