"""
Shared serializer bases.

The important one here is MaskedUserSerializer. ONA's whole proposition is
that it stands between two parties who do not know each other, so a real name
leaking early is not a cosmetic bug — it is the product failing.

Masking is therefore opt-OUT. A serializer that does nothing special returns
'Creative #028'. Showing a real name requires explicitly passing
unmasked=True in the serializer context, which is greppable and reviewable.
"""

from rest_framework import serializers


class MaskedUserSerializer(serializers.ModelSerializer):
    """
    Base for any serializer that exposes a User to another party.

    Returns the masked reference unless the view explicitly grants unmasked
    access:

        # masked — the default
        ProfileSerializer(user)

        # real name — deliberate, and visible in review
        ProfileSerializer(user, context={'unmasked': True})

    WHEN UNMASKING IS LEGITIMATE
        - the user is looking at their own record
        - ONA admin is looking at anyone (they are the intermediary)
        - a project has been approved, so both parties have been introduced

    Everything else stays masked. If you find yourself passing unmasked=True
    to make a test pass, the test is probably asserting the wrong thing.
    """

    display_name = serializers.SerializerMethodField()

    class Meta:
        # Deliberately does not include first_name / last_name / email.
        # A subclass that needs them must add them and justify it.
        fields = ('id', 'display_name', 'role')
        read_only_fields = fields

    def get_display_name(self, obj) -> str:
        if self._is_unmasked(obj):
            return obj.full_name or obj.email
        return obj.masked_name

    def _is_unmasked(self, obj) -> bool:
        """
        Three ways to see a real name. Each is checked explicitly rather than
        inferred, so the reason is visible at the point of decision.
        """
        context = self.context

        # 1. The view said so, having decided the parties are introduced.
        if context.get('unmasked'):
            return True

        request = context.get('request')
        if request is None or not request.user.is_authenticated:
            return False

        # 2. You can always see yourself.
        if request.user.pk == obj.pk:
            return True

        # 3. ONA admin sees everyone — they are the intermediary, and cannot
        #    resolve a dispute between two anonymous references.
        if getattr(request.user, 'role', None) == 'admin':
            return True

        return False


class TimestampedSerializer(serializers.ModelSerializer):
    """Adds created/updated in ISO-8601 UTC, for anything with those fields."""

    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)
