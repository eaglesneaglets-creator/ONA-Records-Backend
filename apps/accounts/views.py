"""
Authentication endpoints.

Tokens are issued by simplejwt. Email verification and password reset use
Django's built-in token generator rather than a bespoke one — it is already
tied to the user's password hash and last_login, so a token stops working the
moment the password changes.
"""

import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.conf import settings
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.utils import timezone
from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from .serializers import (
    EmptySerializer,
    LoginSerializer,
    LogoutSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RegisterSerializer,
    UserSerializer,
    VerifyEmailSerializer,
)

logger = logging.getLogger(__name__)
User = get_user_model()


def _tokens_for(user) -> dict:
    refresh = RefreshToken.for_user(user)
    return {'refresh': str(refresh), 'access': str(refresh.access_token)}


def _send_verification_email(user) -> None:
    """
    Send the verification link.

    Failure here must not fail registration: the account exists, and the user
    can request a new link. Losing the account because an SMTP host was
    briefly down would be worse than an unverified account.
    """
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    link = f'{settings.FRONTEND_URL}/verify-email?uid={uid}&token={token}'

    try:
        send_mail(
            subject='Confirm your email address',
            message=(
                f'Hello{" " + user.first_name if user.first_name else ""},\n\n'
                f'Confirm your email address to finish setting up your ONA Records account:\n\n'
                f'{link}\n\n'
                f'This link expires in 3 days. If you did not create an account, ignore this email.\n\n'
                f'ONA Records'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
    except Exception:
        logger.exception('Verification email failed for user %s', user.pk)


class RegisterView(GenericAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]
    throttle_classes = [AnonRateThrottle]

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        _send_verification_email(user)

        # Tokens are issued immediately. The account works before the email
        # is confirmed — verification gates the things that matter (booking,
        # payouts), not the ability to look around. Blocking everything until
        # an email arrives is how people abandon a signup.
        return Response(
            {'user': UserSerializer(user, context={'request': request}).data,
             'tokens': _tokens_for(user)},
            status=status.HTTP_201_CREATED,
        )


class LoginView(GenericAPIView):
    serializer_class = LoginSerializer
    permission_classes = [AllowAny]
    throttle_classes = [AnonRateThrottle]

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']

        user.last_login = timezone.now()
        user.save(update_fields=['last_login'])

        return Response({
            'user': UserSerializer(user, context={'request': request}).data,
            'tokens': _tokens_for(user),
        })


class LogoutView(GenericAPIView):
    serializer_class = LogoutSerializer
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Blacklist the refresh token.

        The access token stays valid until it expires (30 minutes) — that is
        inherent to stateless JWT. Anything needing immediate revocation must
        check a denylist on each request, which is a cost not worth paying
        for a booking platform.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data['refresh']
        try:
            RefreshToken(token).blacklist()
        except Exception:
            # An invalid or already-blacklisted token is not an error worth
            # surfacing: the caller wanted to be logged out, and they are.
            pass
        return Response(status=status.HTTP_205_RESET_CONTENT)


class MeView(GenericAPIView):
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(self.get_serializer(request.user).data)

    def patch(self, request):
        serializer = self.get_serializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class VerifyEmailView(GenericAPIView):
    serializer_class = VerifyEmailSerializer
    permission_classes = [AllowAny]
    throttle_classes = [AnonRateThrottle]

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        uid = serializer.validated_data['uid']
        token = serializer.validated_data['token']

        try:
            pk = force_str(urlsafe_base64_decode(uid))
            user = User.objects.get(pk=pk)
        except Exception:
            return Response({'detail': 'This verification link is not valid.'},
                            status=status.HTTP_400_BAD_REQUEST)

        if not default_token_generator.check_token(user, token):
            return Response(
                {'detail': 'This verification link has expired or has already been used. '
                           'Request a new one.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not user.is_email_verified:
            user.email_verified_at = timezone.now()
            user.save(update_fields=['email_verified_at'])

        return Response({'detail': 'Email address confirmed.'})


class ResendVerificationView(GenericAPIView):
    serializer_class = EmptySerializer
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if request.user.is_email_verified:
            return Response({'detail': 'This email address is already confirmed.'})
        _send_verification_email(request.user)
        return Response({'detail': 'A new confirmation email is on its way.'})


class PasswordResetRequestView(GenericAPIView):
    serializer_class = PasswordResetRequestSerializer
    permission_classes = [AllowAny]
    throttle_classes = [AnonRateThrottle]

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email'].lower().strip()

        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user:
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            link = f'{settings.FRONTEND_URL}/reset-password?uid={uid}&token={token}'
            try:
                send_mail(
                    subject='Reset your password',
                    message=(
                        f'Someone asked to reset the password for this ONA Records account.\n\n'
                        f'{link}\n\n'
                        f'This link expires in 3 days and can only be used once. '
                        f'If it was not you, ignore this email — your password has not changed.\n\n'
                        f'ONA Records'
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email],
                    fail_silently=False,
                )
            except Exception:
                logger.exception('Password reset email failed for user %s', user.pk)

        # Identical response whether or not the account exists. Otherwise this
        # endpoint becomes a way to check which addresses are registered.
        return Response({
            'detail': 'If an account exists for that address, a reset link has been sent.'
        })


class PasswordResetConfirmView(GenericAPIView):
    serializer_class = PasswordResetConfirmSerializer
    permission_classes = [AllowAny]
    throttle_classes = [AnonRateThrottle]

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = data['user']
        user.set_password(data['password'])
        user.save(update_fields=['password'])

        # Changing the password invalidates the token that was just used,
        # because Django's generator hashes the password into it. That is why
        # a reset link is single-use without any extra bookkeeping.
        return Response({'detail': 'Password updated. You can sign in now.'})
