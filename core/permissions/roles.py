"""
Role-based permissions.

Adapted from the Eagles & Eaglets implementation. The structure carries over;
the roles and the platform-specific rules do not.

The one that matters most here is IsApprovedProfessional. "Is a professional"
and "is a professional ONA has approved" are different questions, and every
marketplace surface depends on the second. A professional who has registered
but not been approved must be invisible to customers — that is the vetting
promise the platform is sold on.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsCustomer(BasePermission):
    message = 'Only customers can do this.'

    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.role == 'customer')


class IsProfessional(BasePermission):
    """
    Any professional, approved or not.

    Use this only for surfaces a professional needs BEFORE approval — their
    own profile, their application status. For anything customer-facing use
    IsApprovedProfessional.
    """

    message = 'Only professionals can do this.'

    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.role == 'professional')


class IsApprovedProfessional(BasePermission):
    """
    A professional ONA has actually approved.

    Gates everything a customer can see or transact with: proposals, listings,
    public profiles, taking work. An unapproved professional can build their
    profile but cannot appear or earn.
    """

    message = 'Your account is still being reviewed by ONA.'

    def has_permission(self, request, view):
        u = request.user
        return bool(
            u
            and u.is_authenticated
            and u.role == 'professional'
            and getattr(u, 'is_approved', False)
        )


class IsONAAdmin(BasePermission):
    """
    ONA staff.

    Deliberately checks role and is_superuser rather than Django's is_staff.
    is_staff only grants Django-admin access and is easy to set for unrelated
    reasons; it should not by itself confer authority over other people's
    money.
    """

    message = 'ONA staff only.'

    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and (u.role == 'admin' or u.is_superuser))


class IsOwner(BasePermission):
    """
    Object-level: you may only touch your own record.

    Resolves the owner from the common field names in this codebase. ONA admin
    is NOT exempt here on purpose — admin access is granted explicitly per
    view via IsONAAdmin, so an ownership check never silently becomes an
    admin bypass.
    """

    message = 'This is not yours.'

    OWNER_FIELDS = ('user', 'customer', 'professional', 'owner', 'created_by')

    def has_object_permission(self, request, view, obj):
        if obj == request.user:
            return True
        for field in self.OWNER_FIELDS:
            owner_id = getattr(obj, f'{field}_id', None)
            if owner_id is not None:
                return owner_id == request.user.id
        return False


class IsOwnerOrONAAdmin(BasePermission):
    """Owner, or ONA staff. The usual pairing for a detail view."""

    message = 'This is not yours.'

    def has_object_permission(self, request, view, obj):
        u = request.user
        if u.is_authenticated and (u.role == 'admin' or u.is_superuser):
            return True
        return IsOwner().has_object_permission(request, view, obj)


class IsPartyToProject(BasePermission):
    """
    Object-level: you are the customer or the professional on this project.

    Everything the two parties share — messages, deliverables, the escrow
    ledger for that project — hangs off this. Nobody else, admin aside, has
    any business reading a conversation between two people.
    """

    message = 'You are not part of this project.'

    def has_object_permission(self, request, view, obj):
        u = request.user
        if not u.is_authenticated:
            return False
        if u.role == 'admin' or u.is_superuser:
            return True

        project = getattr(obj, 'project', obj)
        return u.id in (
            getattr(project, 'customer_id', None),
            getattr(project, 'professional_id', None),
        )


class IsEmailVerified(BasePermission):
    """
    Gate the things that matter behind a confirmed email.

    Registration deliberately issues tokens before verification so people can
    look around. This is what stops them booking or being paid until the
    address is real — reaching someone about a session they have paid for
    requires an address that works.

    SAFE_METHODS pass: reading is not the risk.
    """

    message = 'Confirm your email address first.'

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        u = request.user
        return bool(u and u.is_authenticated and u.is_email_verified)


class IsNotSuspended(BasePermission):
    """
    Deny suspended accounts on their very next request.

    Wire into DEFAULT_PERMISSION_CLASSES. Without it a suspended user keeps
    full API access until their access token expires — and can refresh it —
    because JWT auth never re-reads the database. On a platform where
    suspension usually follows a payment dispute, a 30-minute window of
    continued access is not acceptable.

    Anonymous users PASS, or this would break login and registration.
    """

    message = 'This account has been suspended.'

    def has_permission(self, request, view):
        u = request.user
        if not (u and u.is_authenticated):
            return True
        return u.is_active
