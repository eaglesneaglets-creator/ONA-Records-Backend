from rest_framework import serializers

from core.validators import clean_phone

from .models import ContactDetails, Faq


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


CONTACT_FIELDS = ('phone', 'whatsapp', 'email', 'address', 'directions_url')



class ContactPublicSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactDetails
        fields = CONTACT_FIELDS
        read_only_fields = fields


class ContactAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactDetails
        fields = (*CONTACT_FIELDS, 'updated_at')
        read_only_fields = ('updated_at',)

    def validate_phone(self, value):
        return clean_phone(value)

    def validate_whatsapp(self, value):
        return clean_phone(value)

    def validate_address(self, value):
        return value.strip()
