"""
/api/v1/admin/staff/ — ONA staff accounts, managed from the console.

GET    staff/                     everyone with the admin role
POST   staff/                     add a staff member; they get an email to choose a password
PATCH  staff/{id}/                change name, or is_active (deactivate / reactivate)
POST   staff/{id}/resend-invite/  send the "choose a password" email again

Rules:
  * An address that already belongs to a customer is refused, not converted:
    turning someone's customer account into a staff one is a decision for a
    person on the server, not a form.
  * You cannot deactivate yourself, and the last active admin cannot be
    deactivated — the console must never lock everyone out.
  * Deactivation takes effect at once: authentication refuses inactive users,
    so their current tokens stop working on the next request.
Every change is written to the AuditLog.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.notifications.dispatch import notify_account
from apps.notifications.models import Kind
from core.models import AuditLog
from core.permissions.roles import IsONAAdmin

from .models import Role, User


class StaffMemberSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    is_you = serializers.SerializerMethodField()
    invite_pending = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'name', 'first_name', 'last_name', 'email', 'is_active', 'is_you',
                  'invite_pending', 'last_login', 'date_joined')
        read_only_fields = ('id', 'name', 'email', 'is_you', 'invite_pending', 'last_login',
                            'date_joined')

    def get_name(self, obj) -> str:
        return obj.full_name or obj.email

    def get_is_you(self, obj) -> bool:
        return obj.pk == self.context['request'].user.pk

    def get_invite_pending(self, obj) -> bool:
        """Added, but has not chosen a password yet."""
        return not obj.has_usable_password()

    def validate_first_name(self, value: str) -> str:
        return value.strip()

    def validate_last_name(self, value: str) -> str:
        return value.strip()

    def validate(self, attrs):
        member = self.instance
        if member is not None and attrs.get('is_active') is False and member.is_active:
            if member.pk == self.context['request'].user.pk:
                raise serializers.ValidationError({'is_active': 'You cannot deactivate your own account.'})
            others = User.objects.filter(role=Role.ADMIN, is_active=True).exclude(pk=member.pk)
            if not others.exists():
                raise serializers.ValidationError(
                    {'is_active': 'This is the last active staff account. Add someone else first.'})
        return attrs


class StaffInviteSerializer(serializers.Serializer):
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=80)
    last_name = serializers.CharField(max_length=80, required=False, allow_blank=True, default='')

    def validate_email(self, value: str) -> str:
        value = value.strip().lower()
        existing = User.objects.filter(email__iexact=value).first()
        if existing is not None:
            if existing.role == Role.ADMIN:
                raise serializers.ValidationError('This person is already on the staff list.')
            raise serializers.ValidationError(
                'This address belongs to a customer account. Use a different address for staff work.')
        return value

    def validate_first_name(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Enter their first name.')
        return value


class StaffViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, mixins.UpdateModelMixin,
                   mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated, IsONAAdmin]
    serializer_class = StaffMemberSerializer
    filter_backends = []
    pagination_class = None  # a handful of people, shown at once
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_queryset(self):
        return User.objects.filter(role=Role.ADMIN).order_by('-is_active', 'first_name', 'email')

    def create(self, request, *args, **kwargs):
        invite = StaffInviteSerializer(data=request.data)
        invite.is_valid(raise_exception=True)
        data = invite.validated_data
        try:
            with transaction.atomic():
                # No password: they choose one from the invite email.
                member = User.objects.create_user(
                    email=data['email'], password=None, role=Role.ADMIN,
                    first_name=data['first_name'], last_name=data['last_name'])
        except IntegrityError:
            return Response({'email': ['This address is already in use.']},
                            status=status.HTTP_400_BAD_REQUEST)
        AuditLog.record(AuditLog.Action.STAFF_INVITED, actor=request.user, target=member,
                        request=request, email=member.email)
        notify_account(member, Kind.STAFF_INVITED)
        return Response(self.get_serializer(member).data, status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        was_active = serializer.instance.is_active
        member = serializer.save()
        if was_active != member.is_active:
            act = AuditLog.Action.USER_REACTIVATED if member.is_active else AuditLog.Action.USER_SUSPENDED
            AuditLog.record(act, actor=self.request.user, target=member, request=self.request,
                            email=member.email, scope='staff')

    @action(detail=True, methods=['post'], url_path='resend-invite')
    def resend_invite(self, request, pk=None):
        member = self.get_object()
        if not member.is_active:
            return Response({'detail': 'Reactivate this account before sending an invite.'},
                            status=status.HTTP_409_CONFLICT)
        if member.has_usable_password():
            return Response({'detail': 'They have already chosen a password. They can use '
                                       '"Forgot your password?" if they have lost it.'},
                            status=status.HTTP_409_CONFLICT)
        notify_account(member, Kind.STAFF_INVITED)
        return Response({'detail': f'Invite sent again to {member.email}.'})
