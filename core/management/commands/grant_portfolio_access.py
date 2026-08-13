"""Idempotently grants StaffProfile.is_portfolio_owner (see the portfolio
app) to the fixed set of people who currently qualify — same qualifying
set as grant_company_admin.py, since it's the same person. Runs on every
deploy (see Procfile), safe to re-run."""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from core.models import StaffProfile

QUALIFYING_USERNAMES = ['admin']
QUALIFYING_EMAILS = ['justin@proper-realty.com']


class Command(BaseCommand):
    help = 'Idempotently grants /portfolio/ dashboard access to the initial qualifying user.'

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
            if not profile.is_portfolio_owner:
                profile.is_portfolio_owner = True
                profile.save(update_fields=['is_portfolio_owner'])
            self.stdout.write(self.style.SUCCESS(f'Portfolio access granted: {user.username}'))
