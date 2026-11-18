import os
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase


def run():
    out = StringIO()
    with mock.patch.dict(os.environ, {'ADMIN_PASSWORD': 'pw-for-tests-only', 'ADMIN_USERNAME': 'admin'}):
        call_command('bootstrap_admin', stdout=out)
    return out.getvalue()


class BootstrapAdminTests(TestCase):
    """The shared login is only for a brand-new install: once a real administrator exists it is never brought back."""

    def test_a_new_install_gets_its_one_login(self):
        run()
        admin = User.objects.get(username='admin')
        self.assertTrue(admin.is_superuser and admin.is_active)

    def test_it_is_not_recreated_once_another_active_administrator_exists(self):
        User.objects.create_superuser('owner', 'o@example.com', 'x')
        out = run()
        self.assertFalse(User.objects.filter(username='admin').exists())
        self.assertIn('skipping', out)

    def test_a_deleted_shared_login_stays_deleted_on_the_next_deploy(self):
        User.objects.create_superuser('owner', 'o@example.com', 'x')
        run()
        run()
        self.assertFalse(User.objects.filter(username='admin').exists())

    def test_an_inactive_other_administrator_does_not_count(self):
        gone = User.objects.create_superuser('former', 'f@example.com', 'x')
        gone.is_active = False
        gone.save()
        run()
        self.assertTrue(User.objects.filter(username='admin', is_active=True).exists())

    def test_the_existing_shared_login_is_still_kept_up_when_it_is_the_only_administrator(self):
        run()
        run()
        self.assertEqual(User.objects.filter(username='admin').count(), 1)

    def test_a_deactivated_shared_login_stays_retired(self):
        User.objects.create_user('admin', 'a@example.com', 'x', is_active=False)
        run()
        self.assertFalse(User.objects.get(username='admin').is_active)
