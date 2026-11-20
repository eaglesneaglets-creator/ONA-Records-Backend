"""Small input checks shared by serializers."""

from rest_framework import serializers


def clean_phone(value: str) -> str:
    """A Ghanaian or international phone number, as people type it. Blank is allowed."""
    value = value.strip()
    if not value:
        return value
    if any(ch not in '0123456789+ -()' for ch in value):
        raise serializers.ValidationError('Use digits, spaces and an optional + only.')
    digits = sum(ch.isdigit() for ch in value)
    if not 9 <= digits <= 15:
        raise serializers.ValidationError('Enter a phone number, e.g. 024 123 4567 or +233 24 123 4567.')
    return value
