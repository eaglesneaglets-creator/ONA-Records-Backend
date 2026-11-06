"""
Professionals — admin-managed records, no login (ADR-016).

v0.1 has no professional portal (ADR-015). ONA still brokers professional
work by hand, so bookings need something real to point at when staff assign
a session musician or a mix engineer: this record. Staff create and edit it;
nobody logs in as it; it is never shown publicly.

THE v0.2 OBLIGATION — read before touching `user`
-------------------------------------------------
`user` is null on every record in v0.1, and the admin API refuses to set it.
When professional self-registration ships (v0.2), a registering professional
must CLAIM their existing record rather than create a duplicate: the claim
fills `user`, and from then on the person edits their own profile. That claim
flow needs identity verification — proving the person registering is the
person on the record (e.g. a code sent to the phone/email ONA already holds)
— designed as part of v0.2, never improvised. Until then, a professional who
registers gets the holding page (lib/auth/roles.ts DEFERRED_ROLES) and is not
linked to anything here.

CONTACT DETAILS ARE PRIVATE (PRODUCT.md constraint 1: identities masked until
ONA approves progression). Phone and email exist for staff to reach the
person; no customer-facing serializer may include them. Tests assert this.
"""

from django.conf import settings
from django.db import models

from apps.payments.money import format_ghs
from core.mixins.soft_delete import AllObjectsManager, SoftDeleteManager, SoftDeleteMixin
from core.mixins.timestamp import TimestampMixin, UUIDPrimaryKeyMixin


class RateType(models.TextChoices):
    # Same vocabulary as the v0.2 ServiceListing (docs/data-model.md), so the
    # claim flow can carry the rate across without translation.
    HOURLY = 'hourly', 'Per hour'
    DAILY = 'daily', 'Per day'
    PER_SONG = 'per_song', 'Per song'
    PER_PROJECT = 'per_project', 'Per project'
    PER_EVENT = 'per_event', 'Per event'
    NEGOTIABLE = 'negotiable', 'Negotiable'


class ProfessionalQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)


class Professional(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='professional_record',
        help_text='Always empty in v0.1. Filled only by the v0.2 claim flow — see module docs.',
    )
    name = models.CharField(max_length=150, help_text='The person, as staff know them.')
    stage_name = models.CharField(max_length=150, blank=True)
    skills = models.JSONField(default=list, blank=True,
                              help_text='List of strings, e.g. ["session guitar", "mixing"].')
    rate_type = models.CharField(max_length=12, choices=RateType.choices,
                                 default=RateType.NEGOTIABLE)
    rate_pesewas = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Integer pesewas for one unit of rate_type. Empty when negotiable.')
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)
    notes = models.TextField(blank=True, help_text='Internal. Never shown to customers.')
    is_active = models.BooleanField(default=True, db_index=True,
                                    help_text='Off = not offered for new assignments.')

    objects = SoftDeleteManager.from_queryset(ProfessionalQuerySet)()
    all_objects = AllObjectsManager()

    class Meta:
        ordering = ('name',)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(rate_type='negotiable') | models.Q(rate_pesewas__gt=0),
                name='marketplace_pro_rate_set_unless_negotiable',
            ),
        ]

    # What customers see (PRODUCT.md constraint 1). The real name never leaves
    # ONA; with no stage name, a neutral label stands in (QA F2).
    PUBLIC_FALLBACK = 'ONA session professional'

    def __str__(self) -> str:
        return self.stage_name or self.name

    @property
    def public_name(self) -> str:
        return self.stage_name.strip() or self.PUBLIC_FALLBACK

    @property
    def rate_display(self) -> str:
        if self.rate_type == RateType.NEGOTIABLE or not self.rate_pesewas:
            return 'Negotiable'
        return f'{format_ghs(self.rate_pesewas)} {self.get_rate_type_display().lower()}'


# At most this many skills per record; kept next to the field it guards.
MAX_SKILLS = 20


# ---------------------------------------------------------------------------
# Jobs — brokered work that is not a studio session (Task 15)
# ---------------------------------------------------------------------------

class JobStatus(models.TextChoices):
    REQUESTED = 'requested', 'Requested'      # customer asked; ONA is finding someone
    ARRANGED = 'arranged', 'Arranged'          # professional assigned, terms agreed
    IN_PROGRESS = 'in_progress', 'In progress'
    COMPLETED = 'completed', 'Completed'
    CANCELLED = 'cancelled', 'Cancelled'


class Job(UUIDPrimaryKeyMixin, TimestampMixin):
    """
    A piece of work ONA brokers by hand between a customer and a professional
    — a voice-over, a mix, a video shoot — that is not a booked studio
    session.

    Deliberately lightweight: the v0.3 marketplace replaces this with
    requests, proposals and projects. Until then staff need one place to
    record who asked for what, who is doing it, for how much, and where it
    stands. Payment for a job is recorded with manual payments (Task 16);
    this record carries the agreed fee, never a ledger movement.
    """

    reference = models.CharField(max_length=12, unique=True, editable=False)
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                 related_name='jobs')
    professional = models.ForeignKey(Professional, null=True, blank=True,
                                     on_delete=models.PROTECT, related_name='jobs')
    title = models.CharField(max_length=150)
    brief = models.TextField(blank=True, help_text='What the customer asked for.')
    status = models.CharField(max_length=12, choices=JobStatus.choices,
                              default=JobStatus.REQUESTED, db_index=True)
    due_on = models.DateField(null=True, blank=True)
    fee_pesewas = models.PositiveIntegerField(
        null=True, blank=True, help_text='Agreed price to the customer, integer pesewas.')
    notes = models.TextField(blank=True, help_text='Internal. Never shown to customers.')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name='+')

    class Meta:
        ordering = ('-created_at',)
        constraints = [
            # "Arranged" and beyond means someone is doing it.
            models.CheckConstraint(
                condition=models.Q(status__in=['requested', 'cancelled'])
                | models.Q(professional__isnull=False),
                name='marketplace_job_professional_once_arranged',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.reference} · {self.title}'

    def save(self, *args, **kwargs):
        if not self.reference:
            import secrets
            self.reference = 'JOB-' + secrets.token_hex(3).upper()
        super().save(*args, **kwargs)

    @property
    def fee_display(self) -> str:
        return format_ghs(self.fee_pesewas) if self.fee_pesewas is not None else 'To be agreed'
