"""
Accounts — users and roles.

A custom user model is defined before the first migration on purpose: Django
makes AUTH_USER_MODEL very hard to change once migrations exist.

Email is the login identifier, not a username. Customers and professionals
sign up with an email; a separate username is a field nobody wants to invent.
"""

import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


class Role(models.TextChoices):
    CUSTOMER = 'customer', 'Customer'
    PROFESSIONAL = 'professional', 'Professional'
    ADMIN = 'admin', 'ONA Admin'


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError('An email address is required.')
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault('role', Role.CUSTOMER)
        extra.setdefault('is_staff', False)
        extra.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault('role', Role.ADMIN)
        extra.setdefault('is_staff', True)
        extra.setdefault('is_superuser', True)
        extra.setdefault('is_active', True)
        extra.setdefault('email_verified_at', timezone.now())
        if extra.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')
        if extra.get('role') != Role.ADMIN:
            raise ValueError('Superuser must have role=admin.')
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """
    A platform user in one of three roles.

    PUBLIC REFERENCE
        Customers and professionals never see each other's real names until
        ONA approves a project. `public_ref` is what they see instead —
        "Creative #028", "Customer #201". It is assigned once and never
        changes, so a masked identity stays stable across a conversation.

        Masking is enforced at the serializer layer, not here: the model
        holds the truth, the API decides who may see it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    email = models.EmailField(unique=True, db_index=True)
    phone = models.CharField(max_length=20, blank=True)

    first_name = models.CharField(max_length=80, blank=True)
    last_name = models.CharField(max_length=80, blank=True)

    role = models.CharField(
        max_length=20, choices=Role.choices, default=Role.CUSTOMER, db_index=True,
    )

    # Sequential per role, assigned on creation. Not the primary key: the PK
    # is a UUID so it is not guessable, while this is deliberately readable.
    public_ref = models.PositiveIntegerField(null=True, blank=True, db_index=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    email_verified_at = models.DateTimeField(null=True, blank=True)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    class Meta:
        db_table = 'accounts_user'
        indexes = [models.Index(fields=['role', 'is_active'])]

    def __str__(self):
        return self.email

    @property
    def full_name(self) -> str:
        return ' '.join(p for p in (self.first_name, self.last_name) if p)

    @property
    def masked_name(self) -> str:
        """
        What the other party sees before ONA approves the project.

        Falls back to the role alone if no public_ref has been assigned yet,
        rather than leaking a name.
        """
        label = {
            Role.PROFESSIONAL: 'Creative',
            Role.CUSTOMER: 'Customer',
            Role.ADMIN: 'ONA',
        }.get(self.role, 'User')
        if self.public_ref is None:
            return label
        return '%s #%03d' % (label, self.public_ref)

    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None
