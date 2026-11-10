from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Property, StaffProfile

from .models import Visit, VisitType


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class CleaningPaymentsUnsubmittedTests(TestCase):
    """A cleaning we scheduled a cleaner for that was never submitted shows on the Cleaning payments screen, in red, with no amount and no way to pay it."""

    def setUp(self):
        User = get_user_model()
        self.client.force_login(User.objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        cu = User.objects.create_user('kim', first_name='Kim', password='pw12345678')
        self.cleaner = StaffProfile.objects.create(user=cu, role=StaffProfile.Role.CLEANER)
        self.prop = Property.objects.create(name='324 Lofts')
        self.vt = VisitType.objects.create(name='Commercial Clean')
        self.today = timezone.localdate()

    def visit(self, days_ago, status):
        return Visit.objects.create(property=self.prop, visit_type=self.vt, scheduled_date=self.today - timedelta(days=days_ago), status=status, assigned_staff=self.cleaner)

    def page(self):
        return self.client.get(reverse('onsite_cleaning_payments'))

    def test_past_scheduled_visit_is_listed_as_unsubmitted(self):
        self.visit(5, Visit.Status.SCHEDULED)
        r = self.page()
        self.assertContains(r, 'Unsubmitted')
        self.assertContains(r, 'never submitted')
        self.assertEqual(r.context['unsubmitted_count'], 1)

    def test_not_listed_when_cancelled_future_old_or_submitted(self):
        self.visit(5, Visit.Status.CANCELLED)
        self.visit(5, Visit.Status.SKIPPED)
        self.visit(-3, Visit.Status.SCHEDULED)       # still to come
        self.visit(45, Visit.Status.SCHEDULED)       # older than the 30 days looked back
        r = self.page()
        self.assertEqual(r.context['unsubmitted_count'], 0)
        self.assertNotContains(r, 'never submitted')

    def test_unsubmitted_visit_cannot_be_marked_paid(self):
        v = self.visit(5, Visit.Status.SCHEDULED)
        self.client.post(reverse('onsite_cleaning_payments'), {'action': 'mark_paid', 'visit_ids': [v.pk]})
        v.refresh_from_db()
        self.assertIsNone(v.payment_batch)
