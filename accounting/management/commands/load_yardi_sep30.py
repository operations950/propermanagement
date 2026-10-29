"""Load associations into the books as of Sep 30 2026 from Yardi's figures (accounting/yardi_sep30_data.py; read accounting/yardi_load.py for what that means).

  python manage.py load_yardi_sep30                       look at every association: loads each one, prints what it found, and changes NOTHING
  python manage.py load_yardi_sep30 --apply               load every association that has no books yet (one that already has books is skipped)
  python manage.py load_yardi_sep30 glen                  look at one association (glen, grand, castlewood, la_pensee, lakeside_8, lakeside_2, linton)
  python manage.py load_yardi_sep30 glen --wipe-first --apply     take out St Andrews Glen's books first, then load them again

Owners are matched to the contacts you already have: the same email first, else the same phone, else the same or a close name. An owner who could be more than one
existing contact is listed under TO DECIDE; say which are the same person with  --link "Yardi name=contact id"  (repeat it), say who is a new person with  --new "Yardi name",
or let every undecided owner be a new person with --new-for-undecided. Yardi's email and phone replace what a matched contact had. Proper Realty itself is never loaded as an owner.
--apply will not run while anyone is still undecided.

Without --apply every load is rolled back at the end, so what it prints is exactly what --apply would do.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounting import yardi_load as Y
from accounting.yardi_sep30_data import DATA


class _Rollback(Exception):
    pass


class Command(BaseCommand):
    help = "Load associations into the books as of Sep 30 2026 from Yardi's figures. Looks only, unless --apply."

    def add_arguments(self, parser):
        parser.add_argument('keys', nargs='*', help='which associations (default: all)')
        parser.add_argument('--apply', action='store_true', help='really do it; without this nothing is changed')
        parser.add_argument('--wipe-first', action='store_true', help="take out the association's existing books first")
        parser.add_argument('--link', action='append', default=[], metavar='"NAME=ID"', help='an owner as Yardi names them = the id of the contact they already are (repeat for several)')
        parser.add_argument('--new', action='append', default=[], metavar='"NAME"', help='an owner as Yardi names them who is a NEW person, whatever contact looks like them (repeat for several)')
        parser.add_argument('--new-for-undecided', action='store_true', help='load owners who could be an existing contact as new people')

    def handle(self, *args, **opts):
        keys = opts['keys'] or Y.ORDER
        overrides = {}
        for item in opts['link']:
            if '=' not in item or not item.rsplit('=', 1)[1].strip().isdigit():
                raise CommandError(f'--link needs "Yardi name=contact id" (a number); got "{item}".')
            name, cid = item.rsplit('=', 1)
            overrides[name.strip()] = int(cid)
        for k in keys:
            if k not in DATA:
                raise CommandError(f'Unknown association "{k}". Choose from: {", ".join(Y.ORDER)}.')
        self.stdout.write('LOADING FOR REAL' if opts['apply'] else 'LOOKING ONLY: nothing will be changed (add --apply to do it)')
        failed = 0
        for key in keys:
            self.stdout.write('')
            try:
                with transaction.atomic():
                    prop, _ = Y.find_property(key)
                    if DATA[key]['financials'] and Y.has_books(prop):
                        if not opts['wipe_first']:
                            self.stdout.write(f'{prop.name}: already has books, skipped. (Use --wipe-first to take them out and load again.)')
                            raise _Rollback()
                        for label, n in Y.wipe(prop):
                            if n:
                                self.stdout.write(f'  removed {n} {label}')
                    report = Y.load(key, overrides=overrides, strict=opts['apply'] and not opts['new_for_undecided'], force_new=opts['new'])
                    for line in Y.describe(report):
                        self.stdout.write(line)
                    if not opts['apply']:
                        raise _Rollback()
            except _Rollback:
                continue
            except Y.LoadError as e:
                failed += 1
                self.stdout.write(self.style.ERROR(f'{key}: NOT LOADED: {e}'))
        self.stdout.write('')
        if failed:
            raise CommandError(f'{failed} association(s) could not be loaded; nothing was left half done.')
        self.stdout.write('Done.' if opts['apply'] else 'All of the above would load cleanly. Run again with --apply to do it.')
