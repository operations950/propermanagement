"""Idempotently seeds the two businesses the owner asked for at launch —
Personal and Televend — each with a small starter category set. Personal
additionally gets one example recurring rule (a monthly bill placeholder,
clearly labeled, safe to edit or delete) so the recurring engine has
something to actually demonstrate the first time the dashboard loads.
Safe to re-run: every create is get_or_create, and it never touches a
business/category/rule that already exists by name."""
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import StaffProfile
from portfolio.models import BizRecurringRule, Business, BusinessCategory, Frequency
from tickets.models import Priority


class Command(BaseCommand):
    help = 'Seeds the Personal and Televend businesses with starter categories (idempotent).'

    def handle(self, *args, **options):
        owner = StaffProfile.objects.filter(is_portfolio_owner=True).first()
        if not owner:
            self.stdout.write(self.style.ERROR(
                'No StaffProfile has is_portfolio_owner=True yet — run grant_portfolio_access first.'
            ))
            return

        personal, created = Business.objects.get_or_create(
            name='Personal', defaults={'owner': owner, 'icon': 'home'},
        )
        if created:
            self.stdout.write(self.style.SUCCESS('Created business: Personal'))
        for i, name in enumerate(['Bills', 'Home', 'Family', 'Other']):
            BusinessCategory.objects.get_or_create(business=personal, name=name, defaults={'display_order': i})

        bills_category = BusinessCategory.objects.get(business=personal, name='Bills')
        if not BizRecurringRule.objects.filter(business=personal, title__startswith='Example —').exists():
            BizRecurringRule.objects.create(
                business=personal,
                category=bills_category,
                title='Example — Pay a bill (edit or delete me)',
                notes='Rename this (or delete it) and set the real day it\'s due — this just shows how '
                      'a monthly recurring bill works.',
                priority=Priority.MEDIUM,
                frequency=Frequency.MONTHLY_DAY,
                day_of_month=1,
                next_due_date=timezone.localdate(),
            )
            self.stdout.write(self.style.SUCCESS('Created example recurring rule under Personal'))

        televend, created = Business.objects.get_or_create(
            name='Televend', defaults={'owner': owner, 'icon': 'package'},
        )
        if created:
            self.stdout.write(self.style.SUCCESS('Created business: Televend'))
        for i, name in enumerate(['Operations', 'Sales', 'Admin', 'Other']):
            BusinessCategory.objects.get_or_create(business=televend, name=name, defaults={'display_order': i})

        self.stdout.write(self.style.SUCCESS('Done.'))
