"""Fill each owner's legal name from the Yardi owner list (see accounting/legal_names.py).

    manage.py fill_owner_legal_names              (a dry run: lists what it would set and what it could not match; nothing is written)
    manage.py fill_owner_legal_names --apply

An owner who already has a legal name is left alone. Safe to run again."""
from django.core.management.base import BaseCommand

from accounting import legal_names


class Command(BaseCommand):
    help = "Set owners' legal names from the Yardi owner list (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='write the names (without it, nothing is written)')
        parser.add_argument('--show', action='store_true', help='list every name that would be set, not only the problems')

    def handle(self, *args, **opts):
        proposed = legal_names.plan()
        w = self.stdout.write
        w(f"Would set {len(proposed['set'])} legal names; {proposed['already']} owners already have one; {len(proposed['unmatched'])} could not be matched; {len(proposed['no_yardi'])} are not in the Yardi list.")
        if opts['show']:
            for person, prop, unit, name in proposed['set']:
                w(f'  SET   {prop} {unit}: "{person.name}" -> "{name}"')
        for prop, unit, contact, names in proposed['unmatched']:
            w(f'  NO MATCH  {prop} {unit}: contact "{contact}" vs Yardi {names}')
        for prop, unit, contact in proposed['no_yardi']:
            w(f'  NOT IN YARDI  {prop} {unit}: "{contact}"')
        if opts['apply']:
            w(f'Wrote {legal_names.apply(proposed)} legal names.')
        else:
            w('Dry run: nothing was written. Add --apply to write the names.')
