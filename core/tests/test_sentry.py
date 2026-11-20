"""core/sentry.py: what reaches Sentry is stripped of personal data and secrets."""

from unittest import mock

from core import sentry


def _event():
    return {
        'message': 'Booking for ama@example.com failed',
        'user': {'id': '1', 'email': 'ama@example.com'},
        'request': {
            'url': 'https://api.onaplay.com/api/v1/auth/reset/?uid=MQ&token=abc#x',
            'query_string': 'uid=MQ&token=abc',
            'data': {'password': 'hunter2', 'id_number': 'GHA-123456789-0'},
            'cookies': {'sessionid': 'x'},
            'headers': {'Referer': 'https://www.onaplay.com/reset-password?uid=MQ&token=abc',
                        'User-Agent': 'UA'},
        },
        'logentry': {'message': 'Notification %s to %s failed',
                     'formatted': 'Notification verify to ama@example.com failed',
                     'params': ['verify', 'ama@example.com']},
        'exception': {'values': [
            {'type': 'IntegrityError', 'value': 'Key (email)=(ama@example.com) already exists.'},
            {'type': 'ValueError', 'value': 'MoMo 024 123 4567 / +233 24 123 4567, card GHA-123456789-0'},
        ]},
        'breadcrumbs': {'values': [
            {'category': 'httplib', 'message': None,
             'data': {'url': 'https://kyc.example/v1/x?reviewer=staff@ona.com',
                      'http.query': 'reviewer=staff@ona.com'}},
        ]},
    }


def test_before_send_removes_personal_data_and_secrets():
    out = sentry.before_send(_event(), {})
    flat = repr(out)
    for leaked in ('ama@example.com', 'staff@ona.com', 'hunter2', 'token=abc', 'uid=MQ',
                   '024 123 4567', '+233 24 123 4567', 'GHA-123456789-0', 'sessionid'):
        assert leaked not in flat, leaked
    assert 'user' not in out
    assert out['request']['url'] == 'https://api.onaplay.com/api/v1/auth/reset/'
    assert 'Referer' not in out['request']['headers']
    assert out['request']['headers']['User-Agent'] == 'UA'
    assert out['logentry']['params'] == ['[Filtered]', '[Filtered]']
    assert out['exception']['values'][0]['value'] == 'Key (email)=([email]) already exists.'
    assert out['exception']['values'][0]['type'] == 'IntegrityError'


def test_transaction_spans_lose_queries_template_context_and_cache_keys():
    event = {'type': 'transaction', 'transaction': '/api/v1/admin/kyc/<id>/', 'spans': [
        {'op': 'http.client', 'description': 'GET https://kyc.example/v1/x?reviewer=staff%40ona.com',
         'data': {'url': 'https://kyc.example/v1/x', 'http.query': 'reviewer=staff%40ona.com'}},
        {'op': 'template.render', 'description': 'admin/change_list.html',
         'data': {'context': {'user': 'ama@example.com', 'q': '0241234567'}}},
        {'op': 'cache.get', 'description': 'django.contrib.sessions.cachesecretkey',
         'data': {'cache.key': ['django.contrib.sessions.cachesecretkey']},
         'tags': {'redis.key': 'django.contrib.sessions.cachesecretkey'}},
    ], 'request': {'url': 'https://api.onaplay.com/x', 'headers': {
        'X-Paystack-Signature': 'sig', 'KYC-Signature': 'sig', 'X-Forwarded-For': '41.66.1.1',
        'User-Agent': 'UA'}}}
    out = repr(sentry.before_send(event, {}))
    for leaked in ('staff', 'reviewer', 'ama@example.com', '0241234567', "'sig'", '41.66.1.1', 'cachesecretkey'):
        assert leaked not in out, leaked
    assert "'User-Agent': 'UA'" in out


def test_mask_decodes_percent_encoding():
    assert sentry.mask('reviewer=staff%40ona.com') == 'reviewer=[email]'


def test_redis_breadcrumbs_are_dropped():
    assert sentry.before_breadcrumb({'category': 'redis', 'message': 'GET :1:django.contrib.sessions.cachex'}, {}) is None


def test_before_send_keeps_ordinary_text():
    event = {'message': 'Booking ONA-7K2Q9 cancelled at 10:00', 'exception': {'values': [
        {'type': 'LedgerError', 'value': 'Refund of 20000 pesewas exceeds 15000 held'}]}}
    out = sentry.before_send(event, {})
    assert out['message'] == 'Booking ONA-7K2Q9 cancelled at 10:00'
    assert out['exception']['values'][0]['value'] == 'Refund of 20000 pesewas exceeds 15000 held'


def test_before_breadcrumb_drops_query_strings():
    crumb = sentry.before_breadcrumb(
        {'message': 'sent to kofi@example.com',
         'data': {'url': 'https://api.paystack.co/x?email=kofi@example.com', 'http.query': 'email=kofi'}},
        {})
    assert crumb['data'] == {'url': 'https://api.paystack.co/x'}
    assert crumb['message'] == 'sent to [email]'


def test_environment_follows_the_settings_module(monkeypatch):
    monkeypatch.setenv('DJANGO_SETTINGS_MODULE', 'config.settings.staging')
    assert sentry.default_environment() == 'staging'
    monkeypatch.setenv('DJANGO_SETTINGS_MODULE', 'config.settings.production')
    assert sentry.default_environment() == 'production'


def test_release_is_the_railway_commit(monkeypatch):
    monkeypatch.delenv('SENTRY_RELEASE', raising=False)
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_SHA', 'abc123')
    assert sentry.default_release() == 'abc123'
    monkeypatch.setenv('SENTRY_RELEASE', 'v1')
    assert sentry.default_release() == 'v1'
    monkeypatch.delenv('SENTRY_RELEASE')
    monkeypatch.delenv('RAILWAY_GIT_COMMIT_SHA')
    assert sentry.default_release() is None


def test_init_switches_off_the_leaky_defaults():
    with mock.patch('sentry_sdk.init') as init:
        sentry.init_sentry('https://k@o1.ingest.sentry.io/1', environment='staging',
                           release='abc', traces_sample_rate=0.1)
    opts = init.call_args.kwargs
    assert opts['send_default_pii'] is False
    assert opts['include_local_variables'] is False
    assert opts['max_request_body_size'] == 'never'
    assert opts['trace_propagation_targets'] == []
    assert opts['environment'] == 'staging' and opts['release'] == 'abc'
    assert opts['before_send'] is sentry.before_send
    assert [type(i).__name__ for i in opts['disabled_integrations']] == ['RedisIntegration']
    scrubber = opts['event_scrubber']
    assert scrubber.recursive and {'id_number', 'refresh', 'uid', 'email', 'http.query'} <= set(scrubber.denylist)
