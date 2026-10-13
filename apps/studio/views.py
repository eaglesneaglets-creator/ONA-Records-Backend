"""
Studio endpoints.

CATALOGUE (public, read-only)
    GET /api/v1/studio/rooms/        GET /api/v1/studio/rooms/{id}/
    GET /api/v1/studio/engineers/    GET /api/v1/studio/engineers/{id}/
    GET /api/v1/studio/services/     GET /api/v1/studio/services/{id}/
    GET /api/v1/studio/calendar/

AllowAny on purpose — the project default is IsAuthenticated, and a visitor
needs to see prices and free slots before deciding to register. Read-only:
staff edit the catalogue in admin (and the ONA admin dashboard in Phase 3).
The catalogue queryset is `.active()`, so an inactive or soft-deleted row is
a 404 here even by direct id.

BOOKINGS (customer)
    POST /api/v1/studio/bookings/     GET /api/v1/studio/bookings/[{id}/]
"""

from datetime import date, timedelta

from django.db import IntegrityError, transaction
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions.roles import IsCustomer, IsOwnerOrONAAdmin

from .availability import MAX_RANGE_DAYS, availability
from .models import Engineer, Room, Service, StudioBooking
from .serializers import (
    BookingCreateSerializer, BookingSerializer, EngineerSerializer, RoomSerializer,
    ServiceSerializer, SlotSerializer,
)


class CatalogueViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    search_fields = ('name', 'description')
    ordering_fields = ('name',)

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
    """Validates room/engineer/start/end. Defaults to the next 14 days.
    `from` is a Python keyword, so the view maps from/to onto start/end."""

    room = serializers.PrimaryKeyRelatedField(queryset=Room.objects.active(), required=False)
    engineer = serializers.PrimaryKeyRelatedField(queryset=Engineer.objects.active(),
                                                  required=False)
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


class BookingViewSet(mixins.CreateModelMixin, mixins.ListModelMixin,
                     mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    POST /studio/bookings/        customer creates (no payment yet — Phase 2)
    GET  /studio/bookings/        own bookings; ONA staff see all
    GET  /studio/bookings/{id}/   own, or ONA staff

    Cancel / reschedule arrive with the customer dashboard (Task 12).
    """

    permission_classes = [IsAuthenticated, IsOwnerOrONAAdmin]

    def get_permissions(self):
        if self.action == 'create':
            return [IsAuthenticated(), IsCustomer()]
        return super().get_permissions()

    def get_serializer_class(self):
        return BookingCreateSerializer if self.action == 'create' else BookingSerializer

    def get_queryset(self):
        qs = StudioBooking.objects.select_related('room', 'engineer', 'service')
        u = self.request.user
        if u.role == 'admin' or u.is_superuser:
            return qs
        return qs.filter(customer=u)

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                booking = ser.save()
        except IntegrityError as exc:
            # Validation passed a moment ago; another request won the race
            # and the exclusion constraint refused this one. Same sentence
            # the validator would have given, not a 500.
            which = 'engineer' if 'engineer_overlap' in str(exc) else 'room'
            return Response(
                {'detail': f'That {which} was booked by someone else just now. '
                           'Please pick another time.',
                 'code': 'slot_taken'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(BookingSerializer(booking, context=self.get_serializer_context()).data,
                        status=status.HTTP_201_CREATED)
