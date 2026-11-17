"""
Identity checks through the KYC service.

The KYC service itself is tested in its own repo (unit tests and a Docker
end-to-end run). Here a small fake stands in for it at the HTTP boundary,
so these tests cover what ONA adds: who may call, what reaches KYC, how its
answers and failures come back, the webhook, and the staff relay.
"""

import hashlib
import hmac
import json
import time
import uuid
from unittest import mock

import pytest
import requests
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework.test import APIClient

from apps.kyc import client as kyc
from apps.kyc.models import IdentityCheck
from core.models import AuditLog

pytestmark = pytest.mark.django_db

JPEG = b'\xff\xd8\xff\xe0' + b'x' * 32
SECRET = 'whsec_test'


class FakeKyc:
    """Answers like the KYC service, for the calls ONA makes."""

    def __init__(self):
        self.verifications: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.down = False

    def _resp(self, code, body=None, content=None, ctype='application/json'):
        r = requests.Response()
        r.status_code = code
        r._content = content if content is not None else json.dumps(body or {}).encode()
        r.headers['Content-Type'] = ctype
        return r

    def request(self, method, url, headers=None, timeout=None, json=None, data=None, files=None, params=None):
        path = url.split('/v1', 1)[1]
        self.calls.append((method, path, {'json': json, 'data': data, 'params': params, 'headers': headers,
                                          'files': files}))
        if self.down:
            raise requests.ConnectionError('down')
        parts = [p for p in path.split('/') if p]
        if method == 'POST' and parts == ['verifications']:
            for v in self.verifications.values():
                if v['subject_ref'] == json['subject_ref'] and v['status'] not in ('approved', 'rejected'):
                    return self._resp(200, v)
            vid = str(uuid.uuid4())
            self.verifications[vid] = {
                'id': vid, 'subject_ref': json['subject_ref'], 'level': 'identity', 'status': 'draft',
                'full_name': '', 'id_number_last4': '', 'documents': [], 'decision_reason': '',
                'missing': ['full_name', 'document:selfie'], 'submitted_at': None, 'decided_at': None,
            }
            return self._resp(201, self.verifications[vid])
        if parts[0] == 'verifications':
            v = self.verifications.get(parts[1])
            if v is None:
                return self._resp(404, {'detail': 'Not found.'})
            if method == 'GET':
                return self._resp(200, v)
            if method == 'PATCH':
                if 'id_number' in json and not json['id_number'].upper().startswith('GHA'):
                    return self._resp(400, {'id_number': ['A Ghana Card number looks like GHA-123456789-0.']})
                v['full_name'] = json.get('full_name', v['full_name'])
                return self._resp(200, v)
            if parts[2:] == ['documents']:
                v['documents'].append({'kind': data['kind']})
                return self._resp(201, {'kind': data['kind']})
            if parts[2:] == ['submit']:
                if v['status'] != 'draft':
                    return self._resp(409, {'detail': 'This verification has already been submitted.'})
                v['status'] = 'submitted'
                return self._resp(200, v)
        if parts[:2] == ['review', 'verifications']:
            if len(parts) == 2:
                wanted = params['status'].split(',')
                rows = [{'id': v['id'], 'subject_ref': v['subject_ref'], 'status': v['status'], 'flags': []}
                        for v in self.verifications.values() if v['status'] in wanted]
                return self._resp(200, {'count': len(rows), 'results': rows})
            v = self.verifications[parts[2]]
            if len(parts) == 3:
                return self._resp(200, {**v, 'id_number': 'GHA-123456789-0', 'events': [],
                                        'documents': [{'kind': 'selfie', 'url': 'https://bucket/secret-link'}]})
            if parts[3] == 'documents':
                return self._resp(200, content=JPEG, ctype='image/jpeg')
            if parts[3] == 'decision':
                v['status'] = json['decision']
                v['decision_reason'] = json.get('reason', '')
                return self._resp(200, {'id': v['id'], 'subject_ref': v['subject_ref'], 'status': v['status']})
            if parts[3] == 'start-review':
                v['status'] = 'in_review'
                return self._resp(200, {'id': v['id'], 'subject_ref': v['subject_ref'], 'status': v['status']})
        return self._resp(500)


@pytest.fixture
def fake(settings):
    settings.KYC_API_URL = 'https://kyc.test'
    settings.KYC_API_KEY = 'kyc_abcd1234_secret'
    settings.KYC_WEBHOOK_SECRET = SECRET
    f = FakeKyc()
    with mock.patch.object(kyc.requests, 'request', side_effect=f.request):
        yield f


@pytest.fixture
def users(django_user_model):
    U = django_user_model.objects
    return {
        'ama': U.create_user(email='ama@x.test', password='p-Ass-1', role='customer', first_name='Ama'),
        'pro': U.create_user(email='pro@x.test', password='p-Ass-1', role='professional'),
        'staff': U.create_user(email='staff@x.test', password='p-Ass-1', role='admin'),
    }


def as_(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


URL = '/api/v1/kyc/'


# ── The person's side ────────────────────────────────────────────────────

def test_without_a_kyc_service_the_page_says_not_available(users, settings):
    settings.KYC_API_URL = ''
    r = as_(users['ama']).get(URL)
    assert r.status_code == 200 and r.json() == {'available': False, 'verification': None}


def test_staff_accounts_do_not_verify_and_strangers_are_refused(users, fake):
    assert as_(users['staff']).get(URL).status_code == 403
    assert APIClient().get(URL).status_code == 401


def test_start_sends_only_the_user_id_with_ona_s_key(users, fake):
    r = as_(users['ama']).post(URL)
    assert r.status_code == 201
    v = r.json()['verification']
    method, path, call = fake.calls[-1]
    assert (method, path) == ('POST', '/verifications/')
    assert call['json'] == {'subject_ref': str(users['ama'].pk), 'level': 'identity'}
    assert call['headers']['Authorization'] == 'Bearer kyc_abcd1234_secret'
    # ONA remembers the status, nothing personal.
    check = IdentityCheck.objects.get()
    assert str(check.verification_id) == v['id'] and check.status == 'draft'
    # A second start resumes the same check.
    assert as_(users['ama']).post(URL).json()['verification']['id'] == v['id']


def test_a_professional_can_verify_too(users, fake):
    assert as_(users['pro']).post(URL).status_code == 201


def test_details_errors_come_back_on_the_same_fields(users, fake):
    c = as_(users['ama'])
    assert c.patch(f'{URL}details/', {'id_number': '1'}, format='json').status_code == 404, 'start first'
    c.post(URL)
    r = c.patch(f'{URL}details/', {'id_type': 'ghana_card', 'id_number': '12345'}, format='json')
    assert r.status_code == 400
    assert 'GHA-123456789-0' in json.dumps(r.json())
    r = c.patch(f'{URL}details/', {'full_name': 'Ama Mensah', 'id_number': 'GHA-123456789-0'}, format='json')
    assert r.status_code == 200 and r.json()['verification']['full_name'] == 'Ama Mensah'


def test_uploads_are_checked_then_relayed(users, fake):
    c = as_(users['ama'])
    c.post(URL)
    photo = SimpleUploadedFile('me.jpg', JPEG, content_type='image/jpeg')
    assert c.post(f'{URL}documents/', {'kind': 'passport', 'file': photo}).status_code == 400
    big = SimpleUploadedFile('big.jpg', JPEG + b'0' * kyc.MAX_UPLOAD_BYTES, content_type='image/jpeg')
    assert c.post(f'{URL}documents/', {'kind': 'selfie', 'file': big}).status_code == 400
    assert not [x for x in fake.calls if x[1].endswith('/documents/')], 'refused files never leave ONA'
    photo = SimpleUploadedFile('me.jpg', JPEG, content_type='image/jpeg')
    r = c.post(f'{URL}documents/', {'kind': 'selfie', 'file': photo})
    assert r.status_code == 201 and r.json()['verification']['documents'] == [{'kind': 'selfie'}]


def test_submit_twice_is_a_conflict_and_kyc_being_down_is_a_503(users, fake):
    c = as_(users['ama'])
    c.post(URL)
    assert c.post(f'{URL}submit/').json()['verification']['status'] == 'submitted'
    assert IdentityCheck.objects.get().status == 'submitted'
    r = c.post(f'{URL}submit/')
    assert r.status_code == 409 and 'already been submitted' in json.dumps(r.json())
    fake.down = True
    r = c.get(URL)
    assert r.status_code == 503 and 'not available right now' in json.dumps(r.json())


def test_a_verified_user_cannot_start_again(users, fake):
    c = as_(users['ama'])
    vid = c.post(URL).json()['verification']['id']
    IdentityCheck.objects.filter(pk=vid).update(status='approved')
    assert c.post(URL).status_code == 409


# ── Webhook ──────────────────────────────────────────────────────────────

def signed(body: dict, secret=SECRET, ts=None):
    raw = json.dumps(body).encode()
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f'{ts}.'.encode() + raw, hashlib.sha256).hexdigest()
    return raw, f't={ts},v1={sig}'


def post_hook(raw, header):
    return APIClient().post(reverse('kyc-webhook'), raw, content_type='application/json',
                            HTTP_KYC_SIGNATURE=header)


def test_webhook_refreshes_the_status_from_kyc_not_from_the_message(users, fake):
    vid = as_(users['ama']).post(URL).json()['verification']['id']
    fake.verifications[vid]['status'] = 'approved'
    # The message claims "rejected"; ONA believes KYC's own record.
    raw, header = signed({'type': 'verification.rejected',
                          'data': {'verification_id': vid, 'subject_ref': str(users['ama'].pk),
                                   'status': 'rejected'}})
    assert post_hook(raw, header).status_code == 200
    assert IdentityCheck.objects.get(pk=vid).status == 'approved'


def test_webhook_refuses_bad_signatures_and_replays(users, fake):
    raw, _ = signed({'data': {}})
    assert post_hook(raw, 't=1,v1=00').status_code == 401
    raw, header = signed({'data': {}}, secret='someone-else')
    assert post_hook(raw, header).status_code == 401
    raw, header = signed({'data': {}}, ts=int(time.time()) - 3600)
    assert post_hook(raw, header).status_code == 401


def test_webhook_asks_kyc_to_retry_when_it_cannot_check(users, fake):
    vid = as_(users['ama']).post(URL).json()['verification']['id']
    fake.down = True
    raw, header = signed({'data': {'verification_id': vid, 'subject_ref': str(users['ama'].pk)}})
    assert post_hook(raw, header).status_code == 503


# ── Staff ────────────────────────────────────────────────────────────────

ADMIN = '/api/v1/admin/kyc/'


def submitted(users, fake):
    c = as_(users['ama'])
    vid = c.post(URL).json()['verification']['id']
    c.post(f'{URL}submit/')
    return vid


def test_only_staff_reach_the_review_endpoints(users, fake):
    vid = submitted(users, fake)
    for who in ('ama', 'pro'):
        c = as_(users[who])
        for method, path in [('get', ADMIN), ('get', f'{ADMIN}{vid}/'),
                             ('get', f'{ADMIN}{vid}/documents/selfie/'),
                             ('post', f'{ADMIN}{vid}/start-review/'), ('post', f'{ADMIN}{vid}/decision/')]:
            assert getattr(c, method)(path).status_code == 403, (who, path)


def test_queue_names_the_ona_account(users, fake):
    submitted(users, fake)
    r = as_(users['staff']).get(ADMIN)
    assert r.status_code == 200
    row = r.json()['results'][0]
    assert row['person']['email'] == 'ama@x.test' and row['person']['name'] == 'Ama'
    assert fake.calls[-1][2]['params'] == {'status': 'submitted,in_review', 'page': 1}
    assert as_(users['staff']).get(f'{ADMIN}?filter=bogus').status_code == 400


def test_detail_never_passes_bucket_links_and_scans_come_as_bytes(users, fake):
    vid = submitted(users, fake)
    staff = as_(users['staff'])
    d = staff.get(f'{ADMIN}{vid}/').json()
    assert d['id_number'] == 'GHA-123456789-0' and d['person']['email'] == 'ama@x.test'
    assert 'secret-link' not in json.dumps(d)
    assert fake.calls[-1][2]['params'] == {'reviewer': 'staff@x.test'}
    r = staff.get(f'{ADMIN}{vid}/documents/selfie/')
    assert r.status_code == 200 and r.content == JPEG and r['Content-Type'] == 'image/jpeg'
    assert r['Cache-Control'] == 'no-store, private'
    assert staff.get(f'{ADMIN}{vid}/documents/passport/').status_code == 404


def test_a_decision_names_the_staff_member_and_is_audited(users, fake):
    vid = submitted(users, fake)
    staff = as_(users['staff'])
    assert staff.post(f'{ADMIN}{vid}/start-review/').json()['status'] == 'in_review'
    assert staff.post(f'{ADMIN}{vid}/decision/', {'decision': 'maybe'}, format='json').status_code == 400
    r = staff.post(f'{ADMIN}{vid}/decision/', {'decision': 'changes_requested', 'reason': 'Back is blurred.'},
                   format='json')
    assert r.status_code == 200
    assert fake.calls[-1][2]['json'] == {'decision': 'changes_requested', 'reason': 'Back is blurred.',
                                         'reviewer': 'staff@x.test'}
    check = IdentityCheck.objects.get(pk=vid)
    assert check.status == 'changes_requested' and check.reason == 'Back is blurred.'
    entry = AuditLog.objects.get(action=AuditLog.Action.IDENTITY_DECIDED)
    assert entry.actor == users['staff'] and entry.metadata['decision'] == 'changes_requested'
