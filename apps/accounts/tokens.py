"""
Email-verification tokens, deliberately separate from password-reset tokens.

Using Django's default_token_generator for both had two consequences found in
review:

  1. A "confirm your email" link was also a working password-reset link for
     the token's lifetime — anyone holding a forwarded or archived
     verification email could take over the account.
  2. The default hash includes last_login, so logging in on another device
     silently invalidated the verification link the user had not clicked yet.

This generator has its own salt (so its tokens are useless to the reset
view and vice versa) and hashes what verification actually depends on: the
account, its email, and whether it is already verified. Verifying flips
email_verified_at, which makes the link single-use.
"""

from django.contrib.auth.tokens import PasswordResetTokenGenerator


class EmailVerificationTokenGenerator(PasswordResetTokenGenerator):
    key_salt = 'apps.accounts.tokens.EmailVerificationTokenGenerator'

    def _make_hash_value(self, user, timestamp):
        verified = user.email_verified_at.isoformat() if user.email_verified_at else ''
        return f'{user.pk}{user.email}{verified}{timestamp}'


email_verification_token = EmailVerificationTokenGenerator()
