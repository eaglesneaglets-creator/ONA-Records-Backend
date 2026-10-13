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

from rest_framework import viewsets
from rest_framework.permissions import AllowAny

from .models import Engineer, Room, Service
from .serializers import EngineerSerializer, RoomSerializer, ServiceSerializer


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
