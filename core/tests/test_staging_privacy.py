import pytest
from django.core.exceptions import MiddlewareNotUsed
from django.core.mail import EmailMessage
from django.core.mail.backends.smtp import EmailBackend as SMTPBackend
from django.http import HttpResponse
from django.test import override_settings

from core.email_backends import RedirectingEmailBackend
from core.middleware.staging import NoIndexMiddleware


@override_settings(STAGING_NOINDEX=True)
def test_staging_middleware_marks_every_response_noindex():
    middleware = NoIndexMiddleware(lambda request: HttpResponse('ok'))

    response = middleware(object())

    assert response['X-Robots-Tag'] == 'noindex, nofollow, noarchive'


@override_settings(STAGING_NOINDEX=False)
def test_staging_middleware_is_disabled_outside_staging():
    with pytest.raises(MiddlewareNotUsed):
        NoIndexMiddleware(lambda request: HttpResponse('ok'))


@override_settings(STAGING_EMAIL_REDIRECT_TO='staging@example.com')
def test_staging_email_backend_replaces_all_real_recipients(monkeypatch):
    monkeypatch.setattr(SMTPBackend, 'send_messages', lambda self, messages: len(messages))
    message = EmailMessage(
        subject='Booking confirmed',
        body='Original body',
        to=['customer@example.com'],
        cc=['manager@example.com'],
        bcc=['audit@example.com'],
    )

    sent = RedirectingEmailBackend().send_messages([message])

    assert sent == 1
    assert message.to == ['staging@example.com']
    assert message.cc == []
    assert message.bcc == []
    assert message.extra_headers['X-Original-To'] == 'customer@example.com'
    assert message.extra_headers['X-Original-Cc'] == 'manager@example.com'
    assert message.extra_headers['X-Original-Bcc'] == 'audit@example.com'
    assert 'Original body' in message.body


@override_settings(STAGING_EMAIL_REDIRECT_TO='')
def test_staging_delivers_to_real_recipients_with_a_tagged_subject(monkeypatch):
    monkeypatch.setattr(SMTPBackend, 'send_messages', lambda self, messages: len(messages))
    message = EmailMessage('Confirm your email', 'Body', to=['new-user@example.com'])

    assert RedirectingEmailBackend().send_messages([message]) == 1
    assert message.to == ['new-user@example.com']
    assert message.subject == '[STAGING] Confirm your email'
    assert 'X-Original-To' not in message.extra_headers
    assert message.body == 'Body'


@override_settings(STAGING_EMAIL_REDIRECT_TO='')
def test_the_staging_tag_is_never_doubled(monkeypatch):
    monkeypatch.setattr(SMTPBackend, 'send_messages', lambda self, messages: len(messages))
    message = EmailMessage('[STAGING] Booking confirmed', 'Body', to=['a@example.com'])
    RedirectingEmailBackend().send_messages([message])
    assert message.subject == '[STAGING] Booking confirmed'
