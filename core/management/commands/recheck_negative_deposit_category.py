"""One-off, safe fix for a real bug: a negative trust-account line where the platform itself took money back
(a resolution, an adjustment, a guest credit still naming Airbnb/VRBO) used to fall through core.ledger.suggest()'s
category guesses straight to Expense, so it never showed up as an open item for the income reconciliation to match
against a payout - the money was there in QuickBooks, but invisible here. Only ever touches a line nobody has
reviewed yet (a person's own choice is never overwritten) and only changes it if suggest() now says Deposit where
it used to say Expense. Safe to run more than once."""
from django.core.management.base import BaseCommand

from core import ledger
from core.models import LedgerLine


class Command(BaseCommand):
    help = "Re-suggests the category of unreviewed negative trust lines naming Airbnb/VRBO, fixing ones that fell through to Expense."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually change the lines. Without this, only reports what it would do.')

    def handle(self, *args, **options):
        candidates = LedgerLine.objects.filter(
            role=LedgerLine.Role.TRUST, category=LedgerLine.Category.EXPENSE, reviewed=False, flow__lt=0,
        ).select_related('property', 'unit')
        changed = 0
        for line in candidates:
            category, source = ledger.suggest(ledger.Book(line.property, line.unit), line.role, line.flow, line.split, line.payee, line.memo)
            if category != LedgerLine.Category.DEPOSIT:
                continue
            where = f'{line.property.name}{f" - {line.unit.label}" if line.unit_id else ""}'
            self.stdout.write(f'{where} {line.txn_date} {line.flow}: {line.payee} / {line.memo} -> {category}')
            changed += 1
            if options['apply']:
                line.category, line.category_source = category, source
                line.save(update_fields=['category', 'category_source'])
        if not options['apply']:
            self.stdout.write(self.style.WARNING(f'{changed} line(s) would change (dry run - pass --apply to actually change them).'))
        else:
            self.stdout.write(self.style.SUCCESS(f'{changed} line(s) recoded to Deposit.'))
