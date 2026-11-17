"""Fold one account into another and delete it, in every association and on the company's chart.

    manage.py retire_account 3831 --into 3813            (a dry run: everything is rolled back)
    manage.py retire_account 3831 --into 3813 --apply

Every journal line on the old account is moved to the new one in the same association (its memo says where it was), so the old account is left with no history and can be deleted
instead of hidden. Nothing is posted: the lines keep their entries, dates and amounts, so no total changes, in a closed month or an open one. It refuses to touch an association where the
new account is missing, is a different kind, or where anything else (a budget line, a rule, a bank account) still points at the old account. Safe to run again.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounting.models import Account, JournalLine, MasterAccount


class Rollback(Exception):
    pass


class Command(BaseCommand):
    help = 'Move an account\'s journal lines to another account and delete it everywhere. A dry run unless --apply.'

    def add_arguments(self, parser):
        parser.add_argument('code')
        parser.add_argument('--into', required=True)
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **opts):
        old, new = str(opts['code']), str(opts['into'])
        if old == new:
            raise CommandError('Choose a different account to move it into.')
        try:
            with transaction.atomic():
                self.run(old, new)
                if not opts['apply']:
                    raise Rollback
        except Rollback:
            self.stdout.write(self.style.WARNING('DRY RUN: rolled back. Add --apply to save.'))
            return
        self.stdout.write(self.style.SUCCESS('APPLIED.'))

    def run(self, old, new):
        problems = []
        for acct in Account.objects.filter(code=old).select_related('association').order_by('association__name'):
            target = Account.objects.filter(association=acct.association, code=new).first()
            name = acct.association.name
            if target is None or target.kind != acct.kind:
                problems.append(f'{name}: no account {new} of the same kind to move it into')
                continue
            lines = list(JournalLine.objects.filter(account=acct).select_related('entry'))
            others = []
            for rel in acct._meta.related_objects:
                if rel.related_model is JournalLine:
                    continue
                n = rel.related_model.objects.filter(**{rel.field.name: acct}).count()
                if n:
                    others.append(f'{rel.related_model.__name__} {n}')
            if others:
                problems.append(f'{name}: still used by {", ".join(others)}')
                continue
            for line in lines:
                line.account = target
                line.memo = f'{line.memo} (was {old} {acct.name})'.strip()[:300]
                line.save(update_fields=['account', 'memo'])
            total = sum((l.credit - l.debit for l in lines), 0)
            acct.delete()
            self.stdout.write(f'  {name}: {len(lines)} line(s) moved to {new} (credit total {total}), account {old} deleted')
        if problems:
            raise CommandError('Nothing was changed. ' + '; '.join(problems))
        gone = MasterAccount.objects.filter(code=old).delete()[0]
        self.stdout.write(f'  company chart: {gone} account(s) deleted')
