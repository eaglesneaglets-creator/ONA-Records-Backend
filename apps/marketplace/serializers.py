"""
Staff-only serializers for professional records.

There is deliberately no public serializer for Professional in v0.1. When a
customer needs to see who is assigned to their session, they get
`ProfessionalPublicSerializer` — a display name and skills, never contact
details (PRODUCT.md constraint 1).
"""

from rest_framework import serializers

from apps.accounts.models import User

from .models import MAX_SKILLS, Job, Professional, RateType


class ProfessionalAdminSerializer(serializers.ModelSerializer):
    rate_display = serializers.CharField(read_only=True)
    # Shown so staff can see the v0.2 link once it exists; never writable here.
    user = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Professional
        fields = ('id', 'name', 'stage_name', 'skills', 'rate_type', 'rate_pesewas',
                  'rate_display', 'phone', 'email', 'notes', 'is_active', 'user',
                  'created_at', 'updated_at')
        read_only_fields = ('id', 'user', 'created_at', 'updated_at')

    def validate_skills(self, value):
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            raise serializers.ValidationError('Skills must be a list of words.')
        cleaned = [s.strip() for s in value if s.strip()]
        if len(cleaned) > MAX_SKILLS:
            raise serializers.ValidationError(f'At most {MAX_SKILLS} skills.')
        return cleaned

    def validate(self, attrs):
        rate_type = attrs.get('rate_type', getattr(self.instance, 'rate_type', RateType.NEGOTIABLE))
        rate = attrs.get('rate_pesewas', getattr(self.instance, 'rate_pesewas', None))
        if rate_type == RateType.NEGOTIABLE:
            attrs['rate_pesewas'] = None
        elif not rate:
            raise serializers.ValidationError({'rate_pesewas': 'Set a rate, or choose Negotiable.'})
        return attrs


class ProfessionalPublicSerializer(serializers.ModelSerializer):
    """What a CUSTOMER may see of an assigned professional. Nothing to contact them by."""

    display_name = serializers.SerializerMethodField()

    class Meta:
        model = Professional
        fields = ('id', 'display_name', 'skills')
        read_only_fields = fields

    def get_display_name(self, obj) -> str:
        return obj.public_name


class JobSerializer(serializers.ModelSerializer):
    """Staff view of a brokered job (Task 15). Write with ids; read with briefs."""

    customer = serializers.PrimaryKeyRelatedField(queryset=User.objects.filter(role='customer'))
    professional = serializers.PrimaryKeyRelatedField(
        queryset=Professional.objects.active(), required=False, allow_null=True)
    customer_brief = serializers.SerializerMethodField()
    professional_brief = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    fee_display = serializers.CharField(read_only=True)
    created_by_email = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = ('id', 'reference', 'customer', 'customer_brief', 'professional',
                  'professional_brief', 'title', 'brief', 'status', 'status_display', 'due_on',
                  'fee_pesewas', 'fee_display', 'notes', 'created_by_email', 'created_at',
                  'updated_at')
        read_only_fields = ('id', 'reference', 'created_at', 'updated_at')
        extra_kwargs = {'fee_pesewas': {'min_value': 0, 'max_value': 100_000_000}}

    def get_customer_brief(self, obj) -> dict:
        c = obj.customer
        return {'id': str(c.pk), 'name': c.full_name or c.email, 'email': c.email, 'phone': c.phone}

    def get_professional_brief(self, obj) -> dict | None:
        p = obj.professional
        return {'id': str(p.pk), 'name': p.name, 'stage_name': p.stage_name} if p else None

    def get_created_by_email(self, obj) -> str | None:
        return obj.created_by.email if obj.created_by_id else None

    def validate(self, attrs):
        status = attrs.get('status', getattr(self.instance, 'status', 'requested'))
        pro = attrs['professional'] if 'professional' in attrs else getattr(
            self.instance, 'professional', None)
        if status not in ('requested', 'cancelled') and pro is None:
            raise serializers.ValidationError(
                {'professional': 'Assign a professional before marking the job as arranged.'})
        return attrs
