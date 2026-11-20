"""
Email backends.

RedirectingEmailBackend is staging's email backend.

By default it delivers to the real recipients with "[STAGING]" at the start
of every subject, so QA can test signup, verification and booking emails end
to end and nobody mistakes a staging email for a live one. That is safe only
while staging holds test accounts: never load a copy of production data into
staging without first setting STAGING_EMAIL_REDIRECT_TO.

With STAGING_EMAIL_REDIRECT_TO set, every message goes to that one inbox
instead; the real To/Cc/Bcc are kept in headers and at the top of the body.
"""

from django.conf import settings
from django.core.mail.backends.smtp import EmailBackend as SMTPBackend

STAGING_TAG = '[STAGING] '


class RedirectingEmailBackend(SMTPBackend):
    """Tag every message as staging; optionally send them all to one inbox."""

    def send_messages(self, email_messages):
        redirect_to = getattr(settings, 'STAGING_EMAIL_REDIRECT_TO', '')

        for message in email_messages:
            if not message.subject.startswith(STAGING_TAG):
                message.subject = STAGING_TAG + message.subject
            if not redirect_to:
                continue

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
