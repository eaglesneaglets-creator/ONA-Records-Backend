"""
Email backends.

RedirectingEmailBackend exists for one reason: staging databases tend to hold
copies of real bookings, which means real customer addresses. Without this,
testing the booking flow on staging emails actual customers about sessions
that never happened.

Enable in staging settings:

    EMAIL_BACKEND = 'core.email_backends.RedirectingEmailBackend'
    STAGING_EMAIL_REDIRECT_TO = 'you@example.com'
"""

from django.conf import settings
from django.core.mail.backends.smtp import EmailBackend as SMTPBackend


class RedirectingEmailBackend(SMTPBackend):
    """
    Send every message to one address instead of its real recipients.

    The original To/Cc/Bcc are preserved in headers and prepended to the body,
    so you can still verify the message was addressed correctly — you just
    receive it yourself instead of the customer receiving it.
    """

    def send_messages(self, email_messages):
        redirect_to = getattr(settings, 'STAGING_EMAIL_REDIRECT_TO', '')
        if not redirect_to:
            # Refuse to send rather than fall through to real delivery. A
            # misconfiguration here is exactly the case this class prevents.
            raise ValueError(
                'RedirectingEmailBackend is active but '
                'STAGING_EMAIL_REDIRECT_TO is not set. Refusing to send: '
                'without a redirect address these messages would go to real '
                'recipients.'
            )

        for message in email_messages:
            original = {
                'To': ', '.join(message.to or []),
                'Cc': ', '.join(getattr(message, 'cc', []) or []),
                'Bcc': ', '.join(getattr(message, 'bcc', []) or []),
            }

            note = ['[STAGING] This email was redirected. Real recipients:']
            note += ['  %s: %s' % (k, v) for k, v in original.items() if v]
            message.body = '\n'.join(note) + '\n\n' + '-' * 60 + '\n\n' + message.body

            for header, value in original.items():
                if value:
                    message.extra_headers['X-Original-%s' % header] = value

            message.to = [redirect_to]
            message.cc = []
            message.bcc = []

        return super().send_messages(email_messages)
