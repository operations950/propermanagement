"""Store the lines of a Yardi general ledger export as detail behind the figures carried across from the old system (no journal entries, no balance changes).
    python manage.py load_gl_detail "path/to/generalledger.xlsx"            # checks and shows what it would do, then rolls back
    python manage.py load_gl_detail "path/to/generalledger.xlsx" --apply    # stores it
Export it from Yardi with Book = Cash or Book = Accrual (the book is read from the file); load each book once."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounting import gl_detail


class _Rollback(Exception):
    pass


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument('path')
        parser.add_argument('--apply', action='store_true', help='Store it. Without this nothing is kept.')
        parser.add_argument('--allow-differences', action='store_true', help='Store it even if an income or expense account does not add up to the figure carried across.')

    def handle(self, *args, path, apply=False, allow_differences=False, **opts):
        try:
            book, period, lines = gl_detail.read(path)
            res = gl_detail.check(book, lines)
        except gl_detail.GLError as e:
            raise CommandError(str(e))
        w = self.stdout.write
        w(f'{len(lines)} lines in {res["entries"]} entries, {book} book, {period}.')
        w(f'Unbalanced entries: {len(res["unbalanced"])}.  Accounts an association does not have: {len(res["missing_accounts"])}.  Income/expense accounts that do not add up to the carried figure: {len(res["differences"])}.')
        for k, v in res['unbalanced'][:10]:
            w(f'   unbalanced {k}: {v}')
        for p, c in res['missing_accounts'][:10]:
            w(f'   missing account {c} in {p}')
        for name, code, acct, got, carried in res['differences'][:40]:
            w(f'   {name}: {code} {acct}: lines {got:,.2f}, carried {carried:,.2f}')
        if res['unbalanced'] or res['missing_accounts']:
            raise CommandError('Fix those first: nothing was stored.')
        if res['differences'] and apply and not allow_differences:
            raise CommandError('Some accounts do not add up to the carried figure: nothing was stored (use --allow-differences to store it anyway).')
        try:
            with transaction.atomic():
                counts = gl_detail.load(book, period, lines)
                for name, n in sorted(counts.items()):
                    w(f'   {name}: {n} lines')
                if not apply:
                    raise _Rollback()
        except _Rollback:
            w('DRY RUN: nothing was stored.')
        else:
            w('STORED.')
