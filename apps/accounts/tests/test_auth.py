"""
Authentication tests.

The masking tests matter most. A broken login is obvious in seconds; a
serializer that quietly returns a real name looks completely normal and
breaks the promise the platform is built on.
"""

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def customer():
    return User.objects.create_user(
        email='abena@example.com', password='a-long-enough-password',
        first_name='Abena', last_name='Darko', role='customer', public_ref=201,
    )


@pytest.fixture
def professional():
    return User.objects.create_user(
        email='kofi@example.com', password='a-long-enough-password',
        first_name='Kofi', last_name='Antwi', role='professional', public_ref=28,
    )


@pytest.fixture
def admin_user():
    return User.objects.create_user(
        email='ama@onarecords.com', password='a-long-enough-password',
        first_name='Ama', last_name='Owusu', role='admin',
    )


class TestRegistration:
    def test_creates_a_customer(self, api):
        r = api.post(reverse('accounts:register'), {
            'email': 'new@example.com',
            'password': 'a-long-enough-password',
            'password_confirm': 'a-long-enough-password',
            'first_name': 'New',
            'role': 'customer',
        }, format='json')

        assert r.status_code == 201
        assert 'access' in r.data['tokens']
        assert 'refresh' in r.data['tokens']
        # The password must never come back, in any form.
        assert 'password' not in str(r.data)

    def test_assigns_a_public_reference(self, api):
        api.post(reverse('accounts:register'), {
            'email': 'first@example.com', 'password': 'a-long-enough-password',
            'password_confirm': 'a-long-enough-password', 'role': 'professional',
        }, format='json')
        user = User.objects.get(email='first@example.com')
        assert user.public_ref == 1
        assert user.masked_name == 'Creative #001'

    def test_references_are_sequential_within_a_role(self, api):
        for i, email in enumerate(['a@example.com', 'b@example.com'], start=1):
            api.post(reverse('accounts:register'), {
                'email': email, 'password': 'a-long-enough-password',
                'password_confirm': 'a-long-enough-password', 'role': 'professional',
            }, format='json')
            assert User.objects.get(email=email).public_ref == i

    def test_roles_number_independently(self, api):
        """A customer and a professional can both be #001."""
        for email, role in [('c@example.com', 'customer'), ('p@example.com', 'professional')]:
            api.post(reverse('accounts:register'), {
                'email': email, 'password': 'a-long-enough-password',
                'password_confirm': 'a-long-enough-password', 'role': role,
            }, format='json')
        assert User.objects.get(email='c@example.com').public_ref == 1
        assert User.objects.get(email='p@example.com').public_ref == 1

    def test_cannot_self_register_as_admin(self, api):
        r = api.post(reverse('accounts:register'), {
            'email': 'sneaky@example.com', 'password': 'a-long-enough-password',
            'password_confirm': 'a-long-enough-password', 'role': 'admin',
        }, format='json')
        assert r.status_code == 400
        assert 'role' in r.data['error']['detail']

    def test_rejects_mismatched_passwords(self, api):
        r = api.post(reverse('accounts:register'), {
            'email': 'x@example.com', 'password': 'a-long-enough-password',
            'password_confirm': 'a-different-password',
        }, format='json')
        assert r.status_code == 400

    def test_rejects_a_weak_password(self, api):
        r = api.post(reverse('accounts:register'), {
            'email': 'x@example.com', 'password': 'password',
            'password_confirm': 'password',
        }, format='json')
        assert r.status_code == 400

    def test_rejects_a_password_similar_to_registration_details(self, api):
        r = api.post(reverse('accounts:register'), {
            'email': 'distinctive.person@example.com',
            'password': 'distinctive.person@example.com',
            'password_confirm': 'distinctive.person@example.com',
            'first_name': 'Distinctive',
        }, format='json')

        assert r.status_code == 400
        assert 'password' in r.data['error']['detail']

    def test_rejects_a_duplicate_email(self, api, customer):
        r = api.post(reverse('accounts:register'), {
            'email': 'ABENA@example.com',   # different case, same account
            'password': 'a-long-enough-password',
            'password_confirm': 'a-long-enough-password',
        }, format='json')
        assert r.status_code == 400

    def test_starts_unverified(self, api):
        api.post(reverse('accounts:register'), {
            'email': 'unverified@example.com', 'password': 'a-long-enough-password',
            'password_confirm': 'a-long-enough-password',
        }, format='json')
        assert not User.objects.get(email='unverified@example.com').is_email_verified


class TestLogin:
    def test_succeeds_with_correct_credentials(self, api, customer):
        r = api.post(reverse('accounts:login'), {
            'email': 'abena@example.com', 'password': 'a-long-enough-password',
        }, format='json')
        assert r.status_code == 200
        assert 'access' in r.data['tokens']

    def test_email_is_case_insensitive(self, api, customer):
        r = api.post(reverse('accounts:login'), {
            'email': 'ABENA@EXAMPLE.COM', 'password': 'a-long-enough-password',
        }, format='json')
        assert r.status_code == 200

    def test_rejects_a_wrong_password(self, api, customer):
        r = api.post(reverse('accounts:login'), {
            'email': 'abena@example.com', 'password': 'wrong',
        }, format='json')
        assert r.status_code == 400

    def test_does_not_reveal_whether_an_account_exists(self, api, customer):
        """
        Wrong password and unknown address must be indistinguishable, or the
        endpoint becomes a way to test which emails are registered.
        """
        wrong_password = api.post(reverse('accounts:login'), {
            'email': 'abena@example.com', 'password': 'wrong',
        }, format='json')
        no_account = api.post(reverse('accounts:login'), {
            'email': 'nobody@example.com', 'password': 'wrong',
        }, format='json')

        assert wrong_password.status_code == no_account.status_code
        assert str(wrong_password.data) == str(no_account.data)

    def test_rejects_a_deactivated_account(self, api, customer):
        customer.is_active = False
        customer.save()
        r = api.post(reverse('accounts:login'), {
            'email': 'abena@example.com', 'password': 'a-long-enough-password',
        }, format='json')
        assert r.status_code == 400


class TestIdentityMasking:
    """
    The platform's central promise. These are the tests to be most careful
    about changing.
    """

    def test_a_user_sees_their_own_real_name(self, api, customer):
        api.force_authenticate(customer)
        r = api.get(reverse('accounts:me'))
        assert r.data['display_name'] == 'Abena Darko'
        assert r.data['email'] == 'abena@example.com'

    def test_masked_serializer_hides_a_real_name_by_default(self, professional):
        from apps.accounts.serializers import PublicUserSerializer
        data = PublicUserSerializer(professional).data
        assert data['display_name'] == 'Creative #028'

    def test_masked_serializer_exposes_no_contact_details(self, professional):
        """
        Not just the name. An email or phone leaking is the same failure —
        it lets the parties take the work off-platform.
        """
        from apps.accounts.serializers import PublicUserSerializer
        data = PublicUserSerializer(professional).data
        blob = str(data)
        assert 'kofi@example.com' not in blob
        assert 'Kofi' not in blob
        assert 'Antwi' not in blob

    def test_unmasking_requires_a_deliberate_act(self, professional):
        from apps.accounts.serializers import PublicUserSerializer
        masked = PublicUserSerializer(professional).data
        unmasked = PublicUserSerializer(professional, context={'unmasked': True}).data
        assert masked['display_name'] == 'Creative #028'
        assert unmasked['display_name'] == 'Kofi Antwi'

    def test_ona_admin_sees_real_names(self, api, professional, admin_user):
        """
        ONA is the intermediary and cannot mediate between two anonymous
        references, so admin is exempt.
        """
        from apps.accounts.serializers import PublicUserSerializer

        class _Req:
            user = admin_user

        data = PublicUserSerializer(professional, context={'request': _Req()}).data
        assert data['display_name'] == 'Kofi Antwi'

    def test_one_customer_cannot_unmask_another(self, professional, customer):
        from apps.accounts.serializers import PublicUserSerializer

        class _Req:
            user = customer

        data = PublicUserSerializer(professional, context={'request': _Req()}).data
        assert data['display_name'] == 'Creative #028'

    def test_masked_name_without_a_reference_does_not_leak(self):
        """A user with no public_ref yet falls back to the role, not a name."""
        u = User(email='x@example.com', first_name='Real', last_name='Name',
                 role='professional', public_ref=None)
        assert u.masked_name == 'Creative'
        assert 'Real' not in u.masked_name


class TestMeEndpoint:
    def test_requires_authentication(self, api):
        assert api.get(reverse('accounts:me')).status_code == 401

    def test_returns_the_authenticated_user(self, api, customer):
        api.force_authenticate(customer)
        r = api.get(reverse('accounts:me'))
        assert r.data['email'] == 'abena@example.com'
        assert r.data['role'] == 'customer'
        assert r.data['public_ref'] == 201

    def test_allows_editing_a_name(self, api, customer):
        api.force_authenticate(customer)
        r = api.patch(reverse('accounts:me'), {'first_name': 'Abenaa'}, format='json')
        assert r.status_code == 200
        customer.refresh_from_db()
        assert customer.first_name == 'Abenaa'

    def test_does_not_allow_changing_role(self, api, customer):
        """Otherwise anyone could promote themselves to admin."""
        api.force_authenticate(customer)
        api.patch(reverse('accounts:me'), {'role': 'admin'}, format='json')
        customer.refresh_from_db()
        assert customer.role == 'customer'

    def test_does_not_allow_changing_public_ref(self, api, customer):
        api.force_authenticate(customer)
        api.patch(reverse('accounts:me'), {'public_ref': 999}, format='json')
        customer.refresh_from_db()
        assert customer.public_ref == 201


class TestPasswordReset:
    def test_response_is_identical_for_known_and_unknown_addresses(self, api, customer):
        known = api.post(reverse('accounts:password-reset'),
                         {'email': 'abena@example.com'}, format='json')
        unknown = api.post(reverse('accounts:password-reset'),
                           {'email': 'nobody@example.com'}, format='json')
        assert known.status_code == unknown.status_code == 200
        assert known.data == unknown.data

    def test_a_valid_token_changes_the_password(self, api, customer):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        r = api.post(reverse('accounts:password-reset-confirm'), {
            'uid': urlsafe_base64_encode(force_bytes(customer.pk)),
            'token': default_token_generator.make_token(customer),
            'password': 'a-brand-new-password',
            'password_confirm': 'a-brand-new-password',
        }, format='json')

        assert r.status_code == 200
        customer.refresh_from_db()
        assert customer.check_password('a-brand-new-password')

    def test_a_reset_token_cannot_be_reused(self, api, customer):
        """
        Django hashes the current password into the token, so changing the
        password invalidates it automatically — single use with no extra
        bookkeeping.
        """
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        uid = urlsafe_base64_encode(force_bytes(customer.pk))
        token = default_token_generator.make_token(customer)
        payload = {'uid': uid, 'token': token,
                   'password': 'a-brand-new-password',
                   'password_confirm': 'a-brand-new-password'}

        assert api.post(reverse('accounts:password-reset-confirm'), payload, format='json').status_code == 200
        assert api.post(reverse('accounts:password-reset-confirm'), payload, format='json').status_code == 400

    def test_rejects_a_forged_token(self, api, customer):
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        r = api.post(reverse('accounts:password-reset-confirm'), {
            'uid': urlsafe_base64_encode(force_bytes(customer.pk)),
            'token': 'not-a-real-token',
            'password': 'a-brand-new-password',
            'password_confirm': 'a-brand-new-password',
        }, format='json')
        assert r.status_code == 400

    def test_rejects_a_malformed_user_id(self, api):
        r = api.post(reverse('accounts:password-reset-confirm'), {
            'uid': 'bm90LWEtdXVpZA',
            'token': 'not-a-real-token',
            'password': 'a-brand-new-password',
            'password_confirm': 'a-brand-new-password',
        }, format='json')

        assert r.status_code == 400
        assert 'token' in r.data['error']['detail']

    def test_rejects_a_password_similar_to_the_account(self, api, customer):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        r = api.post(reverse('accounts:password-reset-confirm'), {
            'uid': urlsafe_base64_encode(force_bytes(customer.pk)),
            'token': default_token_generator.make_token(customer),
            'password': 'abena@example.com',
            'password_confirm': 'abena@example.com',
        }, format='json')

        assert r.status_code == 400
        assert 'password' in r.data['error']['detail']


class TestUserManager:
    @pytest.mark.parametrize('field,value', [
        ('is_staff', False),
        ('is_superuser', False),
        ('role', 'customer'),
    ])
    def test_create_superuser_rejects_inconsistent_privileges(self, field, value):
        with pytest.raises(ValueError):
            User.objects.create_superuser(
                email=f'{field}@example.com',
                password='a-long-enough-password',
                **{field: value},
            )


class TestEmailVerification:
    def test_a_valid_token_verifies(self, api, customer):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        assert not customer.is_email_verified
        r = api.post(reverse('accounts:verify-email'), {
            'uid': urlsafe_base64_encode(force_bytes(customer.pk)),
            'token': default_token_generator.make_token(customer),
        }, format='json')

        assert r.status_code == 200
        customer.refresh_from_db()
        assert customer.is_email_verified

    def test_rejects_a_bad_token(self, api, customer):
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        r = api.post(reverse('accounts:verify-email'), {
            'uid': urlsafe_base64_encode(force_bytes(customer.pk)),
            'token': 'nonsense',
        }, format='json')
        assert r.status_code == 400
