"""
Permission and audit tests.

These assert that permissions actually DENY. A permission class that returns
True too often looks identical to a working one until someone reaches
something they should not have.
"""

import pytest
from django.contrib.auth import get_user_model

from core.models import AuditLog
from core.permissions.roles import (
    IsApprovedProfessional,
    IsCustomer,
    IsEmailVerified,
    IsNotSuspended,
    IsONAAdmin,
    IsOwner,
    IsProfessional,
)

User = get_user_model()
pytestmark = pytest.mark.django_db


class _Request:
    """Minimal stand-in for a DRF request."""

    def __init__(self, user, method='POST'):
        self.user = user
        self.method = method


@pytest.fixture
def customer():
    return User.objects.create_user(
        email='c@example.com', password='a-long-enough-password', role='customer',
    )


@pytest.fixture
def professional():
    return User.objects.create_user(
        email='p@example.com', password='a-long-enough-password', role='professional',
    )


@pytest.fixture
def admin_user():
    return User.objects.create_user(
        email='a@example.com', password='a-long-enough-password', role='admin',
    )


class TestRolePermissions:
    def test_customer_permission_admits_customers_only(self, customer, professional, admin_user):
        p = IsCustomer()
        assert p.has_permission(_Request(customer), None) is True
        assert p.has_permission(_Request(professional), None) is False
        assert p.has_permission(_Request(admin_user), None) is False

    def test_professional_permission_admits_professionals_only(self, customer, professional):
        p = IsProfessional()
        assert p.has_permission(_Request(professional), None) is True
        assert p.has_permission(_Request(customer), None) is False

    def test_ona_admin_permission(self, customer, admin_user):
        p = IsONAAdmin()
        assert p.has_permission(_Request(admin_user), None) is True
        assert p.has_permission(_Request(customer), None) is False


class TestApprovedProfessional:
    """
    The distinction the marketplace depends on: a registered professional is
    not the same as one ONA has vetted.
    """

    def test_unapproved_professional_is_denied(self, professional):
        assert IsApprovedProfessional().has_permission(_Request(professional), None) is False

    def test_approved_professional_is_allowed(self, professional, admin_user):
        professional.approve(by=admin_user)
        professional.refresh_from_db()
        assert IsApprovedProfessional().has_permission(_Request(professional), None) is True

    def test_customer_is_denied_even_though_is_approved_is_true(self, customer):
        """
        A customer's is_approved is True by definition, so the class must
        check the role as well — otherwise every customer passes a
        professionals-only gate.
        """
        assert customer.is_approved is True
        assert IsApprovedProfessional().has_permission(_Request(customer), None) is False


class TestEmailVerification:
    def test_unverified_user_cannot_write(self, customer):
        assert IsEmailVerified().has_permission(_Request(customer, 'POST'), None) is False

    def test_unverified_user_can_still_read(self, customer):
        assert IsEmailVerified().has_permission(_Request(customer, 'GET'), None) is True

    def test_verified_user_can_write(self, customer):
        from django.utils import timezone
        customer.email_verified_at = timezone.now()
        customer.save()
        assert IsEmailVerified().has_permission(_Request(customer, 'POST'), None) is True


class TestSuspension:
    def test_suspended_user_is_denied_immediately(self, customer):
        """
        Not on token expiry — on the very next request. Suspension usually
        follows a payment dispute, so a 30-minute grace period is not
        acceptable.
        """
        customer.is_active = False
        customer.save()
        assert IsNotSuspended().has_permission(_Request(customer), None) is False

    def test_active_user_passes(self, customer):
        assert IsNotSuspended().has_permission(_Request(customer), None) is True

    def test_anonymous_passes(self):
        """Or login and registration would break."""
        class Anon:
            is_authenticated = False
        assert IsNotSuspended().has_permission(_Request(Anon()), None) is True


class TestOwnership:
    def test_a_user_owns_themselves(self, customer):
        assert IsOwner().has_object_permission(_Request(customer), None, customer) is True

    def test_a_user_does_not_own_another(self, customer, professional):
        assert IsOwner().has_object_permission(_Request(customer), None, professional) is False


class TestAuditLog:
    def test_records_actor_action_and_target(self, admin_user, professional):
        AuditLog.record(
            AuditLog.Action.PROFESSIONAL_APPROVED,
            actor=admin_user, target=professional, reason='portfolio verified',
        )
        e = AuditLog.objects.get()
        assert e.action == 'professional.approved'
        assert e.actor == admin_user
        assert e.target_type == 'accounts.user'
        assert e.metadata['reason'] == 'portfolio verified'

    def test_survives_the_actor_being_deleted(self, admin_user, professional):
        """
        The record must outlive the account. A deleted admin taking their
        audit trail with them defeats the purpose.
        """
        AuditLog.record(AuditLog.Action.PAYMENT_RELEASED, actor=admin_user,
                        target=professional, amount_pesewas=106250)
        admin_user.delete()

        e = AuditLog.objects.get()
        assert e.actor is None
        assert e.actor_email == 'a@example.com'
        assert e.metadata['amount_pesewas'] == 106250

    def test_unserialisable_metadata_does_not_lose_the_record(self, admin_user):
        """
        An audit write must never take down the action it describes, and it
        should still record WHAT happened even if one metadata value is odd.

        Coercing to repr() keeps the entry — better than dropping the record
        of a payment release because someone logged an unusual object.
        """
        entry = AuditLog.record(
            AuditLog.Action.PAYMENT_RELEASED, actor=admin_user,
            amount_pesewas=106250, weird=object(),
        )
        assert entry is not None
        assert AuditLog.objects.count() == 1
        assert entry.metadata['amount_pesewas'] == '106250'   # coerced with the rest
        assert 'object' in entry.metadata['weird']

    def test_never_raises_even_on_a_hard_failure(self, admin_user):
        """A broken audit write returns None rather than propagating."""
        from unittest.mock import patch
        with patch.object(AuditLog, 'save', side_effect=RuntimeError('db down')):
            assert AuditLog.record(AuditLog.Action.PAYMENT_RELEASED, actor=admin_user) is None
