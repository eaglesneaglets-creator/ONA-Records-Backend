"""
Mirroring ONA's studio calendar into the booking engine.

The engine itself is tested in its own repo (unit, race and Docker end-to-end
tests). Here a small fake stands in for it at the HTTP boundary, so these
tests cover what ONA adds: what is sent when, how a changed or removed row
reaches the engine, the reconcile repair, and that an engine outage never
breaks an ONA save.
"""

import uuid
from datetime import datetime, timedelta
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
import requests
from django.core.management import call_command
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.engine import client, sync
from apps.studio.models import BlockKind, BookingStatus, CalendarBlock, Engineer, Room, Service, StudioBooking

pytestmark = pytest.mark.django_db
TZ = ZoneInfo('Africa/Accra')


class FakeEngine:
    """Answers like the booking engine, for the calls ONA makes."""

    def __init__(self):
        self.resources, self.schedules, self.closures, self.bookings = {}, {}, {}, {}
        self.calls: list[tuple[str, str]] = []
        self.down = False

    def _resp(self, code, body=None):
        r = requests.Response()
        r.status_code = code
        r._content = (requests.compat.json.dumps(body) if body is not None else '').encode()
        return r

    def _overlaps(self, refs, start, end, skip=None):
        for b in self.bookings.values():
            if b['id'] != skip and b['status'] in ('held', 'confirmed') \
                    and {r['ref'] for r in b['resources']} & set(refs) \
                    and parse_datetime(b['start']) < end and start < parse_datetime(b['end']):
                return True
        return False

    def request(self, method, url, headers=None, timeout=None, json=None, params=None):
        path = url.split('/v1', 1)[1]
        self.calls.append((method, path))
        if self.down:
            raise requests.ConnectionError('down')
        parts = [p for p in path.split('/') if p]
        if parts[0] == 'resources':
            if len(parts) == 1:
                return self._resp(200, {'results': list(self.resources.values()), 'next': None})
            if len(parts) == 3:
                self.schedules[parts[1]] = json
                return self._resp(200, json)
            self.resources[parts[1]] = {'ref': parts[1], **json}
            return self._resp(200, self.resources[parts[1]])
        if parts[0] == 'closures':
            if len(parts) == 1:
                return self._resp(200, {'results': list(self.closures.values()), 'next': None})
            if method == 'DELETE':
                self.closures.pop(parts[2], None)
                return self._resp(204)
            self.closures[parts[2]] = {'external_ref': parts[2], **json}
            return self._resp(200, self.closures[parts[2]])
        if parts[0] == 'bookings':
            if len(parts) == 1 and method == 'GET':
                rows = list(self.bookings.values())
                if 'external_ref' in params:
                    rows = [b for b in rows if b['external_ref'] == params['external_ref']]
                if 'status' in params:
                    rows = [b for b in rows if b['status'] in params['status'].split(',')
                            and parse_datetime(b['end']) > parse_datetime(params['from'])]
                return self._resp(200, {'results': rows, 'next': None})
            if len(parts) == 1:
                refs = [r for g in json['requirements'] for r in g['resources']]
                start, end = parse_datetime(json['start']), parse_datetime(json['end'])
                if self._overlaps(refs, start, end):
                    return self._resp(409, {'code': 'slot_unavailable', 'detail': 'That time is not available.'})
                bid = str(uuid.uuid4())
                self.bookings[bid] = {'id': bid, 'external_ref': json['external_ref'], 'start': json['start'],
                                      'end': json['end'], 'status': 'confirmed' if json['confirm'] else 'held',
                                      'resources': [{'ref': r} for r in refs], 'metadata': json['metadata']}
                return self._resp(201, self.bookings[bid])
            b = self.bookings[parts[1]]
            if parts[2] == 'cancel':
                b['status'] = 'cancelled'
            elif parts[2] == 'reschedule':
                refs = [r for g in json['requirements'] for r in g['resources']]
                b.update(start=json['start'], end=json['end'], resources=[{'ref': r} for r in refs])
            return self._resp(200, b)
        return self._resp(500)

    def live(self):
        return {b['external_ref']: b for b in self.bookings.values() if b['status'] in ('held', 'confirmed')}


@pytest.fixture
def engine(settings):
    settings.BOOKING_API_URL = 'https://booking.test'
    settings.BOOKING_API_KEY = 'bk_abcd1234_secret'
    f = FakeEngine()
    with mock.patch.object(client.requests, 'request', side_effect=f.request):
        yield f


@pytest.fixture
def studio(django_user_model):
    return {
        'customer': django_user_model.objects.create_user(email='c@x.test', password='p-Ass-1'),
        'room': Room.objects.create(name='Room A'),
        'eng': Engineer.objects.create(name='Kofi'),
        'svc': Service.objects.create(name='Recording', base_price_pesewas=20000, duration_minutes=60),
    }


def book(w, *, days=3, hour=10, hours=2, status=BookingStatus.PENDING):
    start = datetime.combine(timezone.localdate() + timedelta(days=days), datetime.min.time(), TZ) \
        + timedelta(hours=hour)
    return StudioBooking.objects.create(customer=w['customer'], room=w['room'], engineer=w['eng'],
                                        service=w['svc'], start_at=start, end_at=start + timedelta(hours=hours),
                                        status=status, total_pesewas=40000)


# ── Nothing happens until it is configured ───────────────────────────────

def test_without_settings_nothing_is_sent(settings, django_capture_on_commit_callbacks):
    settings.BOOKING_API_URL = settings.BOOKING_API_KEY = ''
    with mock.patch.object(client.requests, 'request') as req, \
            django_capture_on_commit_callbacks(execute=True) as callbacks:
        Room.objects.create(name='Room Z')
    assert callbacks == [] and not req.called


# ── Changes go across after commit ───────────────────────────────────────

def test_rooms_and_engineers_become_resources_with_studio_hours(engine, studio, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        room = Room.objects.create(name='Room B')
    ref = sync.room_ref(room.pk)
    assert engine.resources[ref] == {'ref': ref, 'kind': 'room', 'name': 'Room B', 'timezone': 'Africa/Accra',
                                     'is_active': True}
    # conftest sets 09:00–21:00 for these tests.
    assert engine.schedules[ref]['weekly'][0] == {'weekday': 0, 'start': '09:00', 'end': '21:00'}
    with django_capture_on_commit_callbacks(execute=True):
        room.soft_delete()
    assert engine.resources[ref]['is_active'] is False


def test_twenty_four_seven_hours(engine, studio, settings):
    settings.STUDIO_OPEN_HOUR, settings.STUDIO_CLOSE_HOUR = 0, 24
    from django.core.cache import cache
    cache.clear()
    assert sync.schedule_payload()['weekly'][6] == {'weekday': 6, 'start': '00:00', 'end': '24:00'}


def test_blocks_become_closures_scoped_like_ona(engine, studio, django_capture_on_commit_callbacks):
    start = timezone.now() + timedelta(days=2)
    with django_capture_on_commit_callbacks(execute=True):
        on_room = CalendarBlock.objects.create(kind=BlockKind.MAINTENANCE, room=studio['room'], start_at=start,
                                               end_at=start + timedelta(hours=3), reason='New desk')
        holiday = CalendarBlock.objects.create(kind=BlockKind.HOLIDAY, start_at=start,
                                               end_at=start + timedelta(days=1), reason='Farmers Day')
    c = engine.closures[sync.block_ref(on_room.pk)]
    assert (c['kind'], c['resources'], c['reason']) == ('maintenance', [sync.room_ref(studio['room'].pk)], 'New desk')
    assert engine.closures[sync.block_ref(holiday.pk)]['resources'] == []   # the whole studio
    with django_capture_on_commit_callbacks(execute=True):
        on_room.delete()
    assert sync.block_ref(on_room.pk) not in engine.closures


def test_a_booking_follows_its_ona_life(engine, studio, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        b = book(studio)
    mirrored = engine.live()[b.reference]
    # Pending in ONA is confirmed in the engine: ONA releases its own unpaid bookings.
    assert mirrored['status'] == 'confirmed' and mirrored['metadata'] == {'ona_status': 'pending'}
    assert sorted(r['ref'] for r in mirrored['resources']) == sorted(
        [sync.room_ref(studio['room'].pk), sync.engineer_ref(studio['eng'].pk)])

    with django_capture_on_commit_callbacks(execute=True):
        b.start_at += timedelta(hours=4)
        b.end_at += timedelta(hours=4)
        b.save()
    assert parse_datetime(engine.live()[b.reference]['start']) == b.start_at

    with django_capture_on_commit_callbacks(execute=True):
        b.status = BookingStatus.CANCELLED
        b.save()
    assert b.reference not in engine.live()


def test_a_deleted_booking_is_released(engine, studio, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        b = book(studio, status=BookingStatus.CONFIRMED)
    with django_capture_on_commit_callbacks(execute=True):
        b.soft_delete()
    assert b.reference not in engine.live()


def test_new_opening_hours_reach_every_resource(engine, studio, django_capture_on_commit_callbacks):
    from core.models import BusinessSettings
    from core.business import rules
    fields = rules().as_dict()
    fields.pop('saved')
    fields.update(studio_open_hour=7, studio_close_hour=23)
    with django_capture_on_commit_callbacks(execute=True):
        BusinessSettings.objects.update_or_create(pk=1, defaults=fields)
    assert engine.schedules[sync.room_ref(studio['room'].pk)]['weekly'][3]['start'] == '07:00'
    assert engine.schedules[sync.engineer_ref(studio['eng'].pk)]['weekly'][3]['end'] == '23:00'


def test_an_engine_outage_never_breaks_an_ona_save(engine, studio, django_capture_on_commit_callbacks):
    engine.down = True
    with django_capture_on_commit_callbacks(execute=True):
        b = book(studio)                       # no exception reaches the caller
    assert StudioBooking.objects.filter(pk=b.pk).exists()
    with pytest.raises(client.BookingEngineUnavailable):
        sync.sync_booking(b)                   # the task sees it, and Celery retries it


# ── The 30-minute repair (and the first import) ──────────────────────────

def test_reconcile_imports_everything_live_then_finds_nothing_to_do(engine, studio):
    live = book(studio)
    confirmed = book(studio, days=4, status=BookingStatus.CONFIRMED)
    book(studio, days=-3, status=BookingStatus.CONFIRMED)               # over: not needed
    book(studio, days=5, status=BookingStatus.CANCELLED)                # not holding time
    CalendarBlock.objects.create(kind=BlockKind.HOLIDAY, start_at=timezone.now() + timedelta(days=9),
                                 end_at=timezone.now() + timedelta(days=10))
    CalendarBlock.objects.create(kind=BlockKind.HOLIDAY, start_at=timezone.now() - timedelta(days=9),
                                 end_at=timezone.now() - timedelta(days=8))          # over: not needed

    dry = sync.reconcile(dry_run=True)
    assert (dry['bookings']['missing'], dry['closures']['created'], dry['resources']['created']) == (2, 1, 2)
    assert engine.bookings == {} and engine.resources == {}

    first = sync.reconcile()
    assert first['bookings']['created'] == 2 and first['closures']['created'] == 1
    assert set(engine.live()) == {live.reference, confirmed.reference}
    assert first['problems'] == []

    again = sync.reconcile()
    assert again['bookings'] == {'unchanged': 2} and again['closures'] == {'unchanged': 1}
    assert again['resources']['unchanged'] == 2


def test_reconcile_repairs_drift_and_reports_what_it_cannot(engine, studio):
    b = book(studio)
    sync.reconcile()
    # A missed signal: ONA moved the booking with a bulk update.
    StudioBooking.objects.filter(pk=b.pk).update(start_at=b.start_at + timedelta(hours=5),
                                                 end_at=b.end_at + timedelta(hours=5))
    # Something in the engine that ONA does not hold.
    stray = {'id': 'stray', 'external_ref': 'ONA-STRAY1', 'status': 'confirmed', 'metadata': {},
             'start': (b.start_at + timedelta(days=1)).isoformat(),
             'end': (b.end_at + timedelta(days=1)).isoformat(),
             'resources': [{'ref': sync.room_ref(studio['room'].pk)}]}
    engine.bookings['stray'] = stray
    report = sync.reconcile()
    assert report['bookings'] == {'moved': 1, 'cancelled': 1}
    assert parse_datetime(engine.live()[b.reference]['start']) == b.start_at + timedelta(hours=5)
    assert engine.bookings['stray']['status'] == 'cancelled'

    # An engine booking ONA does not know sits where a new ONA booking goes.
    clash = book(studio, days=6)
    StudioBooking.objects.filter(pk=clash.pk).update(start_at=clash.start_at)  # (no signal in this test)
    engine.bookings['squatter'] = {**stray, 'id': 'squatter', 'external_ref': '', 'status': 'confirmed',
                                   'start': clash.start_at.isoformat(), 'end': clash.end_at.isoformat()}
    report = sync.reconcile()
    assert report['bookings']['cancelled'] == 1 and report['bookings']['created'] == 1
    assert clash.reference in engine.live()


def test_engine_sync_command(engine, studio):
    book(studio)
    out = StringIO()
    call_command('engine_sync', '--dry-run', stdout=out)
    assert 'DRY RUN' in out.getvalue() and '1 missing' in out.getvalue()
    out = StringIO()
    call_command('engine_sync', stdout=out)
    assert '1 created' in out.getvalue() and 'No problems.' in out.getvalue()
