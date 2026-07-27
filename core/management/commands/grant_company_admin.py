"""Idempotently grants StaffProfile.is_company_admin (see the Owner
Dashboard feature) to the fixed set of people who currently qualify: the
`admin` bootstrap login and whoever holds justin@proper-realty.com. Runs
on every deploy (see Procfile), safe to re-run — further grants/revokes
after this should go through the "Company Admin" toggle on Admin Tools
instead of another command."""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from core.models import StaffProfile

QUALIFYING_USERNAMES = ['admin']
QUALIFYING_EMAILS = ['justin@proper-realty.com']


class Command(BaseCommand):
    help = 'Idempotently grants Company Admin dashboard access to the initial qualifying users.'

    def handle(self, *args, **options):
        users = list(User.objects.filter(username__in=QUALIFYING_USERNAMES))
        for email in QUALIFYING_EMAILS:
            user = User.objects.filter(email__iexact=email).first()
            if user:
                users.append(user)
            else:
                self.stdout.write(f'No user found with email {email} — skipping.')

        for user in users:
            profile, _ = StaffProfile.objects.get_or_create(user=user)
            if not profile.is_company_admin:
                profile.is_company_admin = True
                profile.save(update_fields=['is_company_admin'])
            self.stdout.write(self.style.SUCCESS(f'Company Admin granted: {user.username}'))
