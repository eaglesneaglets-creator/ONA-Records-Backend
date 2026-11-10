from rest_framework import serializers

from .models import Faq


class FaqPublicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Faq
        fields = ('id', 'question', 'answer')
        read_only_fields = fields


class FaqAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = Faq
        fields = ('id', 'question', 'answer', 'is_published', 'sort_order', 'created_at',
                  'updated_at')
        read_only_fields = ('id', 'created_at', 'updated_at')

    def validate_question(self, value):
        return value.strip()

    def validate_answer(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('An answer is required.')
        return value
