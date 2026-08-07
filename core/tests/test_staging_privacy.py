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
def test_staging_email_backend_refuses_to_send_without_redirect(monkeypatch):
    monkeypatch.setattr(SMTPBackend, 'send_messages', lambda self, messages: len(messages))

    with pytest.raises(ValueError, match='Refusing to send'):
        RedirectingEmailBackend().send_messages([
            EmailMessage('Subject', 'Body', to=['customer@example.com'])
        ])
