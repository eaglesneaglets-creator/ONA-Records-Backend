"""
Delete throwaway accounts from a non-production environment.

Test accounts created against staging cannot be removed over the API: there
is no DELETE on /auth/me/, deliberately, because account deletion should not
be a casual request. Doing it by hand through the Django admin is fine once
and error-prone the fifth time, so this exists instead.

    python manage.py delete_test_users staging-check-0812@example.com

Two safety rules, both enforced rather than documented:

1. It refuses to run under production settings. A command whose whole purpose
   is deleting users has no business being available where the users are real.
2. It refuses to delete an admin. Removing the account you administer the
   platform with is not recoverable through the platform.

Deletion is hard, not soft: User does not use SoftDeleteMixin, so the row goes.
That is correct for a test account -- a soft-deleted row would keep occupying
the unique email and the public_ref sequence.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

User = get_user_model()


class Command(BaseCommand):
    help = "Delete test user accounts by email. Refused under production settings."

    def add_arguments(self, parser):
        parser.add_argument(
            "emails",
            nargs="*",
            help="Email addresses to delete.",
        )
        parser.add_argument(
            "--prefix",
            help="Also delete every account whose email starts with this "
                 "(e.g. e2e- for Playwright users).",
        )
        parser.add_argument(
            "--skip-with-payments",
            action="store_true",
            help="Leave out accounts that have payment records (the ledger is "
                 "append-only) and sweep the rest, instead of refusing.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without deleting it.",
        )

    def handle(self, *args, **options):
        module = getattr(settings, "SETTINGS_MODULE", "") or ""
        if "production" in module:
            raise CommandError(
                "Refusing to run under production settings "
                f"({module}). This command deletes users."
            )

        emails = [e.strip().lower() for e in options["emails"]]
        dry_run = options["dry_run"]

        # Fetch before deleting so the report names what actually existed
        # rather than echoing back the arguments.
        if not emails and not options["prefix"]:
            raise CommandError("Give email addresses, --prefix, or both.")
        query = User.objects.filter(email__in=emails)
        if options["prefix"]:
            query = query | User.objects.filter(email__istartswith=options["prefix"])
        found = list(query.distinct())
        found_emails = {u.email for u in found}

        for email in emails:
            if email not in found_emails:
                self.stdout.write(f"  not found, skipping: {email}")

        admins = [u for u in found if u.role == "admin"]
        if admins:
            raise CommandError(
                "Refusing to delete admin account(s): "
                + ", ".join(u.email for u in admins)
            )

        if not found:
            self.stdout.write(self.style.WARNING("Nothing to delete."))
            return

        for user in found:
            self.stdout.write(
                f"  {'would delete' if dry_run else 'deleting'}: "
                f"{user.email} (role={user.role}, ref={user.public_ref})"
            )

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing was deleted."))
            return

        # One transaction: either all the named accounts go or none do, so a
        # failure part-way cannot leave half a cleanup behind.
        with transaction.atomic():
            # Bookings PROTECT their customer (money records must not vanish
            # by cascade). These are test accounts, so remove their bookings
            # outright first — soft-deleting would still block the delete.
            from apps.payments.models import Invoice, Transaction
            from apps.studio.models import StudioBooking
            # Money records are evidence: a test account that ever paid is
            # not swept automatically. Invoices without payment are fine to go.
            paid = set(Transaction.objects.filter(customer__email__in=found_emails)
                       .values_list('customer__email', flat=True))
            if paid and not options.get('skip_with_payments'):
                raise CommandError(
                    'Refusing: some of these accounts have payment records. '
                    'Remove them deliberately, not with a sweep '
                    '(or pass --skip-with-payments to sweep the rest).')
            if paid:
                found_emails = [e for e in found_emails if e not in paid]
                found = [u for u in found if u.email not in paid]
                self.stdout.write(f"  kept {len(paid)} account(s) with payment records")
            # Brokered jobs PROTECT their customer too; test jobs go with them.
            from apps.marketplace.models import Job
            Job.objects.filter(customer__email__in=found_emails).delete()
            Invoice.objects.filter(booking__customer__email__in=found_emails).delete()
            bookings, _ = StudioBooking.all_objects.filter(
                customer__email__in=found_emails).delete()
            if bookings:
                self.stdout.write(f"  removed {bookings} booking row(s)")
            count, _ = User.objects.filter(email__in=found_emails).delete()

        self.stdout.write(
            self.style.SUCCESS(f"Deleted {len(found)} user(s) ({count} rows total).")
        )
