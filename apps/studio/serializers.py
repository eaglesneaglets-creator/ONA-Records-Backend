"""
Public catalogue serializers.

Read-only and deliberately flat. Each money field ships twice — the integer
pesewas the frontend does arithmetic with, and the formatted GHS string it
displays — so no client ever re-implements format_ghs and drifts from the
backend on rounding or thousands separators.

Nothing internal leaks: no deleted_at/deleted_by, no sort_order. The list
is already ordered; exposing the key invites clients to re-sort by it.
"""

from rest_framework import serializers

from .models import Engineer, Room, Service


class RoomSerializer(serializers.ModelSerializer):
    hourly_rate_display = serializers.CharField(read_only=True)

    class Meta:
        model = Room
        fields = ('id', 'name', 'description', 'hourly_rate_pesewas', 'hourly_rate_display',
                  'capacity')
        read_only_fields = fields


class EngineerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Engineer
        fields = ('id', 'name', 'bio', 'specialties')
        read_only_fields = fields


class ServiceSerializer(serializers.ModelSerializer):
    base_price_display = serializers.CharField(read_only=True)

    class Meta:
        model = Service
        fields = ('id', 'name', 'description', 'base_price_pesewas', 'base_price_display',
                  'duration_minutes')
        read_only_fields = fields


class SlotSerializer(serializers.Serializer):
    start_at = serializers.DateTimeField()
    end_at = serializers.DateTimeField()
    state = serializers.ChoiceField(choices=['available', 'booked', 'maintenance', 'holiday'])
