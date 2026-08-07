"""
Account serializers.

Registration is the one place a user picks their role, and it is deliberately
restricted: you may sign up as a customer or a professional, never as an
admin. ONA staff are created through the admin, not through a public endpoint.
"""

from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers

from core.serializers import MaskedUserSerializer

User = get_user_model()

# Roles a person may choose for themselves. 'admin' is absent on purpose.
SELF_SERVICE_ROLES = ('customer', 'professional')


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, style={'input_type': 'password'})
    password_confirm = serializers.CharField(write_only=True, style={'input_type': 'password'})
    role = serializers.ChoiceField(choices=SELF_SERVICE_ROLES, default='customer')

    class Meta:
        model = User
        fields = ('email', 'password', 'password_confirm', 'first_name', 'last_name', 'phone', 'role')

    def validate_email(self, value):
        value = value.lower().strip()
        if User.objects.filter(email__iexact=value).exists():
            # Deliberately explicit. Hiding this to prevent account
            # enumeration would push the problem to the login screen, where
            # the person cannot tell "wrong password" from "no account" and
            # gives up. The trade-off favours the honest majority.
            raise serializers.ValidationError('An account with this email already exists.')
        return value

    def validate(self, attrs):
        if attrs['password'] != attrs.pop('password_confirm'):
            raise serializers.ValidationError({'password_confirm': 'The two passwords do not match.'})

        # Attribute-similarity validation only works when Django receives a
        # user-shaped object. Passing the password alone silently disables
        # checks against the email and names supplied during registration.
        candidate = User(
            email=attrs.get('email', ''),
            first_name=attrs.get('first_name', ''),
            last_name=attrs.get('last_name', ''),
        )
        try:
            validate_password(attrs['password'], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)})

        return attrs

    def create(self, validated_data):
        password = validated_data.pop('password')
        user = User(**validated_data)
        user.set_password(password)
        user.is_active = True

        # Assign the next public reference for this role. Professionals and
        # customers number independently, so both start at #001.
        #
        # This is a read-then-write race under concurrency. It is acceptable
        # here because public_ref is a display label, not a key — a duplicate
        # is cosmetic, not corrupting. If registration volume ever makes that
        # visible, move it to a sequence.
        last = (
            User.objects.filter(role=user.role, public_ref__isnull=False)
            .order_by('-public_ref')
            .values_list('public_ref', flat=True)
            .first()
        )
        user.public_ref = (last or 0) + 1

        user.save()
        return user


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, style={'input_type': 'password'})

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get('request'),
            username=attrs['email'].lower().strip(),
            password=attrs['password'],
        )

        if user is None:
            # One message for both wrong-email and wrong-password. Here the
            # trade-off flips: at login, distinguishing them would let anyone
            # test whether an address has an account.
            raise serializers.ValidationError('Email or password is incorrect.')

        if not user.is_active:
            raise serializers.ValidationError(
                'This account has been deactivated. Contact ONA if you think that is wrong.'
            )

        attrs['user'] = user
        return attrs


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class UserSerializer(MaskedUserSerializer):
    """
    The authenticated user's own record.

    Inherits the masking base, but adds the private fields — which is safe
    because this serializer is only ever used for `request.user`. The base
    class recognises self-access and unmasks automatically.
    """

    class Meta(MaskedUserSerializer.Meta):
        model = User
        fields = MaskedUserSerializer.Meta.fields + (
            'email', 'first_name', 'last_name', 'phone',
            'public_ref', 'is_email_verified', 'date_joined',
        )
        read_only_fields = ('id', 'display_name', 'role', 'email',
                            'public_ref', 'is_email_verified', 'date_joined')


class PublicUserSerializer(MaskedUserSerializer):
    """
    Another party's record. Masked unless the view says otherwise.

    Adds no private fields at all — that is the point. If a view needs to
    show a real name it passes unmasked=True, and display_name resolves to
    the real one without exposing email or phone.
    """

    class Meta(MaskedUserSerializer.Meta):
        model = User


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()

    # No validation that the address exists. The view responds identically
    # either way, so this endpoint cannot be used to discover accounts.


class VerifyEmailSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()


class EmptySerializer(serializers.Serializer):
    """Documents endpoints that intentionally accept no request body."""


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    password = serializers.CharField(write_only=True, style={'input_type': 'password'})
    password_confirm = serializers.CharField(write_only=True, style={'input_type': 'password'})

    def validate(self, attrs):
        if attrs['password'] != attrs.pop('password_confirm'):
            raise serializers.ValidationError({'password_confirm': 'The two passwords do not match.'})

        try:
            pk = force_str(urlsafe_base64_decode(attrs['uid']))
            user = User.objects.get(pk=pk)
        except (DjangoValidationError, ValueError, TypeError, OverflowError, User.DoesNotExist):
            raise serializers.ValidationError({'token': 'This reset link is not valid.'})

        if not default_token_generator.check_token(user, attrs['token']):
            raise serializers.ValidationError({
                'token': 'This reset link has expired or has already been used. Request a new one.'
            })

        try:
            validate_password(attrs['password'], user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)})

        attrs['user'] = user
        return attrs
