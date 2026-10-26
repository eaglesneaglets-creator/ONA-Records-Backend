"""
Task 15 — manual brokering: staff book for a customer (existing or new),
assign professionals to bookings, and record brokered jobs.

What must hold:
  - the result is indistinguishable to the customer from a self-service booking
  - staff skip the anti-squatting guards, never the physics (past, overlap)
  - assigning a professional re-prices before payment and is frozen after
  - every staff action is audited with the staff member as actor
"""

from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APIClient

from apps.marketplace.models import Job, Professional, RateType
from apps.payments import ledger
from apps.payments.models import Invoice
from apps.payments.pdf import render_invoice
from apps.studio.models import BookingStatus, Engineer, Room, Service, StudioBooking
from core.models import AuditLog

pytestmark = pytest.mark.django_db

TZ = ZoneInfo('Africa/Accra')


def at(days, hour):
    d = date.today() + timedelta(days=days)
    return datetime.combine(d, datetime.min.time(), tzinfo=TZ) + timedelta(hours=hour)


@pytest.fixture
def w(django_user_model):
    U = django_user_model.objects
    return {
        'admin': U.create_user(email='staff@x.test', password='p-Ass-1', role='admin'),
        'ama': U.create_user(email='ama@x.test', password='p-Ass-1', role='customer',
                             first_name='Ama'),
        'room': Room.objects.create(name='A', hourly_rate_pesewas=20000),
        'eng': Engineer.objects.create(name='One'),
        'eng_2': Engineer.objects.create(name='Two'),
        'svc': Service.objects.create(name='Rec', base_price_pesewas=1, duration_minutes=60),
        'pro': Professional.objects.create(
            name='Kwesi Mensah', stage_name='KweSound', skills=['guitar'],
            rate_type=RateType.PER_SONG, rate_pesewas=30000, phone='0244123456',
            email='kwesi@private.test'),
    }


def client(user=None):
    c = APIClient()
    if user is not None:
        c.force_authenticate(user)
    return c


@pytest.fixture
def staff(w):
    return client(w['admin'])


def u(name, *args):
    return reverse(f'adminapi:{name}', args=args)


def body(w, **over):
    data = {'customer': str(w['ama'].pk), 'room': str(w['room'].pk), 'engineer': str(w['eng'].pk),
            'service': str(w['svc'].pk), 'start_at': at(5, 10).isoformat(), 'hours': 2}
    data.update(over)
    return data


def make(w, start, hours=2, **kw):
    return StudioBooking.objects.create(
        customer=w['ama'], room=w['room'], engineer=w['eng'], service=w['svc'], start_at=start,
        end_at=start + timedelta(hours=hours), total_pesewas=20000 * hours, **kw)


class TestAccess:
    def test_customers_cannot_broker(self, w):
        b = make(w, at(5, 10))
        c = client(w['ama'])
        assert c.post(u('booking-list'), body(w), format='json').status_code == 403
        assert c.post(u('booking-assign', b.pk), {'professional': None},
                      format='json').status_code == 403
        assert c.get(u('job-list')).status_code == 403
        assert c.post(u('job-list'), {}, format='json').status_code == 403
        assert StudioBooking.objects.count() == 1 and Job.objects.count() == 0


class TestStaffBooksForACustomer:
    def test_the_customer_sees_an_ordinary_booking(
            self, staff, w, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            res = staff.post(u('booking-list'), body(w, notes='Bring a cajon'), format='json')
        assert res.status_code == 201, res.data
        b = StudioBooking.objects.get(pk=res.data['id'])
        assert b.customer == w['ama'] and b.created_by == w['admin']
        assert b.status == BookingStatus.PENDING and b.total_pesewas == 40000
        assert Invoice.objects.filter(booking=b).exists()

        # The customer's own API returns it like any other booking.
        mine = client(w['ama']).get(reverse('studio:booking-list'), {'when': 'upcoming'}).data
        assert [r['reference'] for r in mine['results']] == [b.reference]
        assert 'created_by' not in mine['results'][0]

        assert [m.subject for m in mail.outbox] == [f'Booking received — {b.reference}']
        log = AuditLog.objects.get(action='booking.created_by_staff')
        assert log.actor == w['admin'] and log.metadata['customer'] == 'ama@x.test'
        assert res.data['created_by_email'] == 'staff@x.test'

    def test_staff_can_start_it_confirmed(self, staff, w):
        res = staff.post(u('booking-list'), body(w, status='confirmed'), format='json')
        assert res.status_code == 201 and res.data['status'] == 'confirmed'

    def test_a_new_walk_in_customer_gets_a_set_password_email(
            self, staff, w, django_capture_on_commit_callbacks):
        data = body(w)
        del data['customer']
        data['new_customer'] = {'email': ' Kofi@Walk.In ', 'first_name': 'Kofi',
                                'phone': '0200000000'}
        with django_capture_on_commit_callbacks(execute=True):
            res = staff.post(u('booking-list'), data, format='json')
        assert res.status_code == 201, res.data
        kofi = StudioBooking.objects.get(pk=res.data['id']).customer
        assert kofi.email == 'kofi@walk.in' and kofi.role == 'customer'
        assert not kofi.has_usable_password()
        subjects = sorted(m.subject for m in mail.outbox)
        assert subjects == sorted(['ONA Records has set up your account',
                                   f'Booking received — {res.data["reference"]}'])
        welcome = next(m for m in mail.outbox if 'set up' in m.subject)
        assert '/reset-password?uid=' in welcome.body and '&welcome=1' in welcome.body
        assert AuditLog.objects.filter(action='customer.created_by_staff', actor=w['admin']).exists()

    def test_a_new_customer_with_a_known_email_is_refused(self, staff, w):
        data = body(w)
        del data['customer']
        data['new_customer'] = {'email': 'AMA@x.test', 'first_name': 'Ama'}
        res = staff.post(u('booking-list'), data, format='json')
        assert res.status_code == 400 and 'already exists' in str(res.data)

    @pytest.mark.parametrize('both', [True, False])
    def test_exactly_one_kind_of_customer(self, staff, w, both):
        data = body(w)
        if both:
            data['new_customer'] = {'email': 'x@y.test', 'first_name': 'X'}
        else:
            del data['customer']
        res = staff.post(u('booking-list'), data, format='json')
        assert res.status_code == 400 and 'customer' in str(res.data)


class TestStaffRules:
    def test_no_pending_cap_and_no_horizon(self, staff, w):
        for i in range(4):
            assert staff.post(u('booking-list'), body(w, start_at=at(5 + i, 10).isoformat()),
                              format='json').status_code == 201
        far = staff.post(u('booking-list'), body(w, start_at=at(200, 10).isoformat()),
                         format='json')
        assert far.status_code == 201, far.data

    def test_outside_hours_needs_an_explicit_yes(self, staff, w):
        late = body(w, start_at=at(5, 20).isoformat(), hours=3)  # 20:00–23:00
        res = staff.post(u('booking-list'), late, format='json')
        assert res.status_code == 400 and 'outside_hours' in str(res.data)
        res = staff.post(u('booking-list'), {**late, 'allow_outside_hours': True}, format='json')
        assert res.status_code == 201, res.data
        assert AuditLog.objects.get(action='booking.created_by_staff').metadata['outside_hours']

    def test_early_start_is_outside_hours_too(self, staff, w):
        res = staff.post(u('booking-list'), body(w, start_at=at(5, 7).isoformat()), format='json')
        assert 'outside_hours' in str(res.data)

    @pytest.mark.parametrize('start', ['past', 'half-past'])
    def test_physics_binds_staff_too(self, staff, w, start):
        when = at(-1, 10) if start == 'past' else at(5, 10) + timedelta(minutes=30)
        res = staff.post(u('booking-list'), body(w, start_at=when.isoformat()), format='json')
        assert res.status_code == 400 and 'start_at' in res.data.get('error', res.data).__str__()

    def test_staff_cannot_double_book(self, staff, w):
        make(w, at(5, 11))
        res = staff.post(u('booking-list'), body(w), format='json')  # 10:00–12:00 overlaps
        assert res.status_code == 400 and 'start_at' in str(res.data)

    def test_a_lost_race_is_a_409_not_a_500(self, staff, w):
        make(w, at(5, 10))
        with mock.patch('apps.studio.brokering.is_free', return_value=(True, '')):
            res = staff.post(u('booking-list'), body(w), format='json')
        assert res.status_code == 409

    def test_a_fee_needs_a_professional(self, staff, w):
        res = staff.post(u('booking-list'), body(w, professional_fee_pesewas=5000), format='json')
        assert res.status_code == 400 and 'professional_fee_pesewas' in str(res.data)


class TestProfessionalOnABooking:
    def test_booked_with_a_professional_the_invoice_has_two_lines(self, staff, w):
        res = staff.post(u('booking-list'), body(w, professional=str(w['pro'].pk),
                                                 professional_fee_pesewas=30000), format='json')
        assert res.status_code == 201, res.data
        b = StudioBooking.objects.get(pk=res.data['id'])
        assert b.total_pesewas == 70000 and b.room_total_pesewas == 40000
        inv = Invoice.objects.get(booking=b)
        assert [line['total_pesewas'] for line in inv.lines] == [40000, 30000]
        assert inv.lines[1]['description'] == 'Session professional · KweSound'

        # The customer sees the stage name and fee — never contact details.
        mine = client(w['ama']).get(reverse('studio:booking-detail', args=[b.pk])).data
        assert mine['professional'] == {'id': str(w['pro'].pk), 'display_name': 'KweSound',
                                        'skills': ['guitar']}
        assert mine['professional_fee_display'] == 'GHS 300.00'
        assert mine['total_display'] == 'GHS 700.00'
        for secret in ('0244123456', 'kwesi@private.test', 'Mensah'):
            assert secret not in str(mine)

    def test_assigning_later_revises_the_invoice_and_tells_the_customer(
            self, staff, w, django_capture_on_commit_callbacks):
        b = make(w, at(5, 10))
        from apps.payments.documents import issue_invoice
        number = issue_invoice(b).number
        with django_capture_on_commit_callbacks(execute=True):
            res = staff.post(u('booking-assign', b.pk), {'professional': str(w['pro'].pk),
                                                       'professional_fee_pesewas': 25000},
                             format='json')
        assert res.status_code == 200, res.data
        assert res.data['assigned_professional']['name'] == 'Kwesi Mensah'
        assert res.data['total_pesewas'] == 65000 and res.data['room_total_display'] == 'GHS 400.00'

        inv = Invoice.objects.get(booking=b)
        assert inv.number == number and inv.revision == 2 and inv.total_pesewas == 65000
        assert b'REVISED INVOICE' in render_invoice(inv)

        [email] = mail.outbox
        assert email.subject == f'Your booking has been updated — {b.reference}'
        assert 'KweSound (GHS 250.00)' in email.body and email.attachments
        log = AuditLog.objects.get(action='booking.professional_assigned')
        assert log.actor == w['admin']
        assert (log.metadata['previous_total_pesewas'], log.metadata['total_pesewas']) == (40000, 65000)

        # Saving the same thing again changes nothing and sends nothing.
        with django_capture_on_commit_callbacks(execute=True):
            staff.post(u('booking-assign', b.pk), {'professional': str(w['pro'].pk),
                                                 'professional_fee_pesewas': 25000}, format='json')
        assert len(mail.outbox) == 1 and Invoice.objects.get(booking=b).revision == 2

        # Removing the professional removes the fee.
        res = staff.post(u('booking-assign', b.pk), {'professional': None}, format='json')
        assert res.data['assigned_professional'] is None and res.data['total_pesewas'] == 40000
        assert Invoice.objects.get(booking=b).revision == 3

    def test_frozen_once_paid(self, staff, w):
        b = make(w, at(5, 10))
        txn = ledger.open_transaction(b)
        ledger.mark_succeeded(txn, provider_ref='HUB-9')
        res = staff.post(u('booking-assign', b.pk), {'professional': str(w['pro'].pk),
                                                   'professional_fee_pesewas': 1000}, format='json')
        assert res.status_code == 400 and 'paid for' in res.data['detail']
        b.refresh_from_db()
        assert b.total_pesewas == 40000 and b.assigned_professional is None

    def test_not_on_a_cancelled_booking(self, staff, w):
        b = make(w, at(5, 10), status=BookingStatus.CANCELLED)
        res = staff.post(u('booking-assign', b.pk), {'professional': str(w['pro'].pk)},
                         format='json')
        assert res.status_code == 400

    def test_an_inactive_professional_cannot_be_assigned(self, staff, w):
        w['pro'].is_active = False
        w['pro'].save()
        b = make(w, at(5, 10))
        res = staff.post(u('booking-assign', b.pk), {'professional': str(w['pro'].pk)},
                         format='json')
        assert res.status_code == 400

    def test_a_fee_without_a_professional_is_refused(self, staff, w):
        b = make(w, at(5, 10))
        res = staff.post(u('booking-assign', b.pk), {'professional': None,
                                                   'professional_fee_pesewas': 100},
                         format='json')
        assert res.status_code == 400


class TestJobs:
    def test_create_arrange_and_complete_are_audited(self, staff, w):
        res = staff.post(u('job-list'), {'customer': str(w['ama'].pk), 'title': 'Radio jingle',
                                         'brief': '30 seconds, upbeat'}, format='json')
        assert res.status_code == 201, res.data
        assert res.data['reference'].startswith('JOB-') and res.data['status'] == 'requested'
        assert res.data['fee_display'] == 'To be agreed'
        jid = res.data['id']

        # Arranged needs a professional.
        res = staff.patch(u('job-detail', jid), {'status': 'arranged'}, format='json')
        assert res.status_code == 400 and 'professional' in str(res.data)
        res = staff.patch(u('job-detail', jid), {'status': 'arranged', 'professional': str(w['pro'].pk),
                                                 'fee_pesewas': 50000}, format='json')
        assert res.status_code == 200, res.data
        assert res.data['professional_brief']['stage_name'] == 'KweSound'
        assert res.data['fee_display'] == 'GHS 500.00'
        assert res.data['created_by_email'] == 'staff@x.test'

        actions = list(AuditLog.objects.filter(actor=w['admin']).values_list('action', flat=True))
        assert {'job.created', 'job.updated'} <= set(actions)
        upd = AuditLog.objects.get(action='job.updated')
        assert upd.metadata['previous_status'] == 'requested' and upd.metadata['status'] == 'arranged'

    def test_jobs_are_cancelled_not_deleted(self, staff, w):
        job = Job.objects.create(customer=w['ama'], title='Mix')
        assert staff.delete(u('job-detail', job.pk)).status_code == 405
        assert Job.objects.filter(pk=job.pk).exists()

    def test_search_and_filter(self, staff, w):
        Job.objects.create(customer=w['ama'], title='Radio jingle')
        Job.objects.create(customer=w['ama'], title='Album cover', status='cancelled')
        assert staff.get(u('job-list'), {'search': 'jingle'}).data['count'] == 1
        assert staff.get(u('job-list'), {'status': 'cancelled'}).data['count'] == 1


class TestSetPassword:
    def test_using_the_link_confirms_the_email(self, w, django_user_model):
        kofi = django_user_model.objects.create_user(email='kofi@walk.in', password=None,
                                                     role='customer')
        assert not kofi.is_email_verified
        res = client().post(reverse('accounts:password-reset-confirm'), {
            'uid': urlsafe_base64_encode(force_bytes(kofi.pk)),
            'token': default_token_generator.make_token(kofi),
            'password': 'A-good-Passw0rd!', 'password_confirm': 'A-good-Passw0rd!'})
        assert res.status_code == 200, res.data
        kofi.refresh_from_db()
        assert kofi.is_email_verified and kofi.check_password('A-good-Passw0rd!')


class TestReviseInvoice:
    def test_issues_one_when_none_exists_yet(self, w):
        from apps.payments.documents import revise_invoice
        b = make(w, at(5, 10))
        inv = revise_invoice(b)
        assert inv.revision == 1 and inv.total_pesewas == 40000

    def test_refuses_once_paid(self, w):
        from apps.payments.documents import DocumentError, issue_invoice, revise_invoice
        b = make(w, at(5, 10))
        issue_invoice(b)
        ledger.mark_succeeded(ledger.open_transaction(b), provider_ref='HUB-R')
        b.assigned_professional, b.professional_fee_pesewas = w['pro'], 100
        b.total_pesewas += 100
        b.save()
        with pytest.raises(DocumentError):
            revise_invoice(b)
        assert Invoice.objects.get(booking=b).revision == 1
