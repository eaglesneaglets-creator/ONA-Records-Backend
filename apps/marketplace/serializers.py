"""
Staff-only serializers for professional records.

There is deliberately no public serializer for Professional in v0.1. When a
customer needs to see who is assigned to their session, they get
`ProfessionalPublicSerializer` — a display name and skills, never contact
details (PRODUCT.md constraint 1).
"""

from rest_framework import serializers

from .models import MAX_SKILLS, Professional, RateType


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
        return obj.stage_name or obj.name
