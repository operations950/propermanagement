"""Creates (or updates) a single admin login from ADMIN_USERNAME/ADMIN_PASSWORD
env vars. Runs on every deploy (see Procfile) so production always has a
working login without seeding any of seed_demo_data's fictional business
data. No-op if ADMIN_PASSWORD isn't set, so it's safe on environments that
haven't opted in.
"""
import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from core.models import StaffProfile


class Command(BaseCommand):
    help = 'Idempotently creates/updates the admin login from ADMIN_USERNAME/ADMIN_PASSWORD env vars.'

    def handle(self, *args, **options):
        password = os.environ.get('ADMIN_PASSWORD')
        if not password:
            self.stdout.write('ADMIN_PASSWORD not set — skipping admin bootstrap.')
            return

        username = os.environ.get('ADMIN_USERNAME', 'admin')
        # This exists so a brand-new install has one working login. Once any other active administrator exists the job is done: recreating the shared
        # login on every deploy would quietly bring back a login someone removed on purpose.
        if User.objects.filter(is_active=True, is_superuser=True).exclude(username=username).exists():
            self.stdout.write('Another active administrator exists — skipping admin bootstrap.')
            return
        existing = User.objects.filter(username=username).first()
        if existing is not None and not existing.is_active:
            # Someone deactivated this login on purpose. Recreating its
            # privileges and password on every deploy would quietly undo that.
            self.stdout.write(f'Admin login {username!r} is deactivated — leaving it retired.')
            return
        user, _ = User.objects.get_or_create(username=username)
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()
        StaffProfile.objects.get_or_create(user=user, defaults={'role': StaffProfile.Role.ADMIN})
        self.stdout.write(self.style.SUCCESS(f'Admin login ready: {username}'))
