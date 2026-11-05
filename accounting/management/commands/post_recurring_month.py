"""A one-time catch-up: posts, for a month that has gone by, each fixed recurring bill that has no bill in it, on the bill's day of the month.

The usual posting does this by itself from the day a recurring bill is set up, so this is only for the first month or two after they are loaded (the loader dates each bill's first
bill in the future, so nothing posts for the month before). It looks first: with no flag it only reports. `--apply` does it. Safe to run again: a month that already has a bill for
a recurring bill is left alone. A bill whose amount changes is not posted here; it is made from the bank statement line when the statement is read.
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from accounting import closing, ledger, payables
from accounting.models import RecurringBill
from core.models import Property


class Command(BaseCommand):
    help = 'Posts the fixed recurring bills of a past month that have no bill in it. Reports only, unless --apply is given.'

    def add_arguments(self, parser):
        parser.add_argument('--month', required=True, help='The month to post, as YYYY-MM.')
        parser.add_argument('--apply', action='store_true', help='Make the changes (without it, only report).')

    def handle(self, *args, **options):
        try:
            year, month = (int(x) for x in options['month'].split('-'))
            first = ledger.month_start(timezone.localdate().replace(year=year, month=month, day=1))
        except ValueError:
            raise CommandError('Give the month as YYYY-MM, for example 2026-10.')
        if first > ledger.month_start(timezone.localdate()):
            raise CommandError('That month has not started yet: the usual posting will do it on the day.')
        apply = options['apply']
        self.stdout.write(self.style.WARNING('APPLYING' if apply else 'PREVIEW ONLY: nothing is written. Run again with --apply to post.'))
        books_on = {p.property_id for p in closing.associations()}
        total = 0
        for prop in Property.objects.filter(pk__in=RecurringBill.objects.filter(is_active=True).values('association')).order_by('name'):
            if prop.pk not in books_on:
                self.stdout.write(f'\n== {prop.name}: the books are not switched on, so nothing is posted')
                continue
            rows = payables.post_month(prop, first, apply=apply)
            self.stdout.write(f'\n== {prop.name}')
            for r in rows:
                rec = r['rec']
                state = ('posted' if r['bill'] is not None else 'NOT POSTED (the month is closed)') if apply else 'will post'
                self.stdout.write(f'   {state}  {rec.vendor.name} - {rec.description}: ${rec.amount:,.2f} dated {r["day"]:%b} {r["day"].day}')
                total += 1 if (r['bill'] is not None or not apply) else 0
            if not rows:
                self.stdout.write('   nothing to post: every fixed bill already has one this month')
        self.stdout.write(self.style.SUCCESS(f'\n{"Posted" if apply else "Would post"} {total} bills for {first:%B %Y}.'))
