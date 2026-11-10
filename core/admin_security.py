"""
Django admin sign-in with the same per-account lockout as the API (Task 18).

Wired in config/urls.py (`admin.site.login_form`). The admin also lives at a
non-default path per environment (ADMIN_URL), so it is not where scanners
look first.
"""

from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm

from core import lockout


class LockoutAdminAuthenticationForm(AdminAuthenticationForm):
    def clean(self):
        email = self.data.get('username', '')
        if lockout.is_locked(email):
            raise forms.ValidationError(lockout.LOCKED_MESSAGE, code='locked')
        try:
            cleaned = super().clean()
        except forms.ValidationError:
            lockout.record_failure(email, request=self.request)
            raise
        lockout.clear(email)
        return cleaned
