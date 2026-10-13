"""
Public catalogue endpoints.

    GET /api/v1/studio/rooms/        GET /api/v1/studio/rooms/{id}/
    GET /api/v1/studio/engineers/    GET /api/v1/studio/engineers/{id}/
    GET /api/v1/studio/services/     GET /api/v1/studio/services/{id}/

AllowAny on purpose — the project default is IsAuthenticated, and a visitor
needs to see prices before deciding to register. Read-only: staff edit the
catalogue in admin (and the ONA admin dashboard in Phase 3), never through
these routes.

The queryset is `.active()`, so an inactive or soft-deleted row is a 404
here even by direct id. The booking endpoint (Task 6) will re-check this at
write time; this is only what the customer can see.
"""

from datetime import date, timedelta

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, viewsets
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .availability import MAX_RANGE_DAYS, availability
from .models import Engineer, Room, Service
from .serializers import EngineerSerializer, RoomSerializer, ServiceSerializer, SlotSerializer


class CatalogueViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    search_fields = ('name', 'description')
    ordering_fields = ('name',)
    # No DjangoFilterBackend fields declared on purpose: the only filter a
    # customer needs is "what can I book", and that is the queryset itself.

    def get_queryset(self):
        return self.model.objects.active()


class RoomViewSet(CatalogueViewSet):
    model = Room
    serializer_class = RoomSerializer


class EngineerViewSet(CatalogueViewSet):
    model = Engineer
    serializer_class = EngineerSerializer
    search_fields = ('name', 'bio')


class ServiceViewSet(CatalogueViewSet):
    model = Service
    serializer_class = ServiceSerializer


class CalendarQuerySerializer(serializers.Serializer):
    """Validates ?from=&to=&room=&engineer=. Defaults to the next 14 days."""

    room = serializers.PrimaryKeyRelatedField(queryset=Room.objects.active(), required=False)
    engineer = serializers.PrimaryKeyRelatedField(queryset=Engineer.objects.active(),
                                                  required=False)
    # `from` is a Python keyword, so the query params are mapped onto
    # start/end in the view before validation.
    start = serializers.DateField(required=False)
    end = serializers.DateField(required=False)

    def validate(self, attrs):
        start = attrs.get('start') or date.today()
        end = attrs.get('end') or start + timedelta(days=13)
        if end < start:
            raise serializers.ValidationError({'to': 'Must not be before "from".'})
        if (end - start).days >= MAX_RANGE_DAYS:
            raise serializers.ValidationError({'to': f'At most {MAX_RANGE_DAYS} days at a time.'})
        attrs['start'], attrs['end'] = start, end
        return attrs


class CalendarView(APIView):
    """
    GET /api/v1/studio/calendar/?room=<id>&engineer=<id>&from=YYYY-MM-DD&to=YYYY-MM-DD

    Hour slots inside opening hours, each tagged available / booked /
    maintenance / holiday. Public: a visitor picks a date before registering.
    Never includes who booked a slot.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        parameters=[
            OpenApiParameter('room', str, description='Room id'),
            OpenApiParameter('engineer', str, description='Engineer id'),
            OpenApiParameter('from', str, description='YYYY-MM-DD, default today'),
            OpenApiParameter('to', str, description='YYYY-MM-DD, default from+13'),
        ],
        responses=SlotSerializer(many=True),
    )
    def get(self, request):
        params = request.query_params
        data = {k: params[k] for k in ('room', 'engineer') if params.get(k)}
        if params.get('from'):
            data['start'] = params['from']
        if params.get('to'):
            data['end'] = params['to']
        query = CalendarQuerySerializer(data=data)
        query.is_valid(raise_exception=True)
        q = query.validated_data
        slots = availability(start=q['start'], end=q['end'],
                             room=q.get('room'), engineer=q.get('engineer'))
        return Response({
            'from': q['start'], 'to': q['end'],
            'room': str(q['room'].id) if q.get('room') else None,
            'engineer': str(q['engineer'].id) if q.get('engineer') else None,
            'slots': SlotSerializer([s.as_dict() for s in slots], many=True).data,
        })
